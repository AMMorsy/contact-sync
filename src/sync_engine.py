from normalizer import normalize_contact
from state_manager import get_last_sync_writes, upsert_dedup_cache, flag_duplicate, record_sync_write
from logger import get_logger
from datetime import datetime, timezone
from rapidfuzz import fuzz
import re

logger = get_logger()


def build_airtable_fields(contact, account):
    return {
        "Dedup Phone":       contact.get("dedup_phone") or "",
        "Clean Phone":       contact.get("clean_phone") or "",
        "Phone":             contact.get("raw_phone") or "",
        "Phone 2":           contact.get("phone2") or "",
        "Phone 3":           contact.get("phone3") or "",
        "Clean Phone 2":     contact.get("phone2") or "",
        "All Phones Raw":    contact.get("all_phones_raw") or "",
        "Clean Email":       contact.get("clean_email") or "",
        "Email":             contact.get("raw_email") or "",
        "First Name":        contact.get("first_name") or "",
        "Last Name":         contact.get("last_name") or "",
        "Full Name":         contact.get("full_name") or "",
        "Company":           contact.get("company") or "",
        "Google Contact ID": contact.get("google_contact_id") or "",
        "Sync Source":       account.get("sync_source_label", "Google Account 1"),
        "Origin":            "google_contacts",
        "Last Synced At":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Sync Lock":         False
    }


def fields_have_changed(existing_fields, new_fields):
    """Only check the 4 watched fields plus Google Contact ID for changes.
    Returns True only if a watched field genuinely differs."""
    check_keys = ["First Name", "Last Name", "Clean Email", "Google Contact ID"]
    for key in check_keys:
        existing = (existing_fields.get(key) or "").strip()
        new      = (new_fields.get(key) or "").strip()
        if existing != new:
            return True
    return False


def build_minimal_patch(existing_fields, new_fields, record_id=None):
    """Build a patch dict containing ONLY the fields that differ.

    USER-EDIT PROTECTION: Only First Name and Last Name are user-editable
    fields that matter for Google sync. For these, check sync_writes to detect
    if the user edited Airtable since our last write — if so, don't overwrite.

    Other fields (Google Contact ID, Clean Email) are system fields that the
    user does not edit directly, so they update freely whenever Google differs."""
    from state_manager import get_last_sync_writes

    # User-editable fields (must be protected from overwrite)
    user_editable = ["First Name", "Last Name"]
    # System fields (update freely from Google)
    system_fields = ["Clean Email", "Google Contact ID"]

    last_writes = get_last_sync_writes(record_id) if record_id else {}
    patch = {}

    # User-editable: protect from overwrite if user edited since last sync write
    for key in user_editable:
        existing = (existing_fields.get(key) or "").strip()
        new      = (new_fields.get(key)      or "").strip()
        if existing == new:
            continue
        if last_writes:
            last_value = (last_writes.get(key) or "").strip()
            if existing != last_value:
                # User edited Airtable since our last write — protect it
                logger.info(
                    f"[D1 SKIP] {record_id} field '{key}': Airtable user edit "
                    f"({existing!r}) vs last_write ({last_value!r}); preserving"
                )
                continue
        patch[key] = new_fields.get(key, "")

    # System fields: just update if different
    for key in system_fields:
        existing = (existing_fields.get(key) or "").strip()
        new      = (new_fields.get(key)      or "").strip()
        if existing != new:
            patch[key] = new_fields.get(key, "")

    # Always update sync metadata
    patch["Sync Source"]    = new_fields.get("Sync Source", "Google Account 1")
    patch["Last Synced At"] = new_fields.get("Last Synced At")
    patch["Sync Lock"]      = False
    return patch


def is_email_as_name(name):
    if not name:
        return True
    if "@" in name:
        return True
    if re.match(r'^[a-zA-Z0-9._%+\-]+\.[a-zA-Z]{2,}$', name.strip()):
        return True
    return False


def build_lookup_maps(airtable_records):
    phone_map = {}
    email_map = {}
    google_id_map = {}
    for record in airtable_records:
        fields = record.get("fields", {})
        gid = (fields.get("Google Contact ID") or "").strip()
        if gid and gid not in google_id_map:
            google_id_map[gid] = record
        for phone_field in ["Dedup Phone", "Phone 2", "Phone 3"]:
            phone = (fields.get(phone_field) or "").strip()
            if phone and phone not in phone_map:
                phone_map[phone] = record
        email = (fields.get("Clean Email") or "").strip().lower()
        if email and email not in email_map:
            email_map[email] = record
    logger.info(
        f"Lookup maps built - google_ids: {len(google_id_map)} | "
        f"phones: {len(phone_map)} | emails: {len(email_map)}"
    )
    return phone_map, email_map, google_id_map


def check_duplicate_local(contact, phone_map, email_map, google_id_map):
    """Find existing Airtable record matching this Google contact.
    Priority: 1) Google Contact ID  2) Phone  3) Email (exact only - no fuzzy)."""
    dedup_phone     = contact.get("dedup_phone")
    phone2          = contact.get("phone2")
    phone3          = contact.get("phone3")
    canonical_email = contact.get("clean_email")
    google_id       = contact.get("google_contact_id")

    # Layer 1: Google Contact ID
    if google_id and google_id in google_id_map:
        record = google_id_map[google_id]
        return "existing", record["id"], "exact_google_id"

    # Layer 2: Phone match
    for phone in [dedup_phone, phone2, phone3]:
        if phone and phone in phone_map:
            record = phone_map[phone]
            return "existing", record["id"], "exact_phone"

    # Layer 3: Email exact match (no fuzzy logic)
    if canonical_email and canonical_email in email_map:
        record = email_map[canonical_email]
        return "existing", record["id"], "exact_email"

    return "new", None, None


def process_contacts(raw_contacts, airtable_client, account, dry_run=False):
    stats = {
        "pulled": 0, "created": 0,
        "updated": 0, "skipped": 0, "errors": 0
    }

    # Skip Airtable load entirely if nothing came from Google
    actionable = [
        r for r in raw_contacts
        if not r.get("metadata", {}).get("deleted", False)
    ]
    if not actionable:
        logger.info("No contacts to process — skipping Airtable load")
        return stats

    # Load ALL Airtable records once
    logger.info("Loading all Airtable records into memory...")
    try:
        all_airtable_records = airtable_client.get_all_records()
        logger.info(f"Loaded {len(all_airtable_records)} Airtable records")
    except Exception as e:
        logger.error(f"Failed to load Airtable records: {e}")
        return stats

    phone_map, email_map, google_id_map = build_lookup_maps(all_airtable_records)

    for raw in raw_contacts:
        stats["pulled"] += 1
        try:
            contact = normalize_contact(raw)

            if contact.get("deleted"):
                logger.info(f"Skipping deleted: {contact.get('google_contact_id')}")
                stats["skipped"] += 1
                continue

            if not contact["has_phone"] and not contact["has_email"]:
                logger.debug(f"Skipping no-phone no-email: {contact.get('full_name')}")
                stats["skipped"] += 1
                continue

            if contact.get("is_ambiguous"):
                logger.warning(
                    f"AMBIGUOUS phone — locking: "
                    f"{contact.get('full_name')} | {contact.get('all_phones_raw')}"
                )
                if not dry_run:
                    status, record_id, _ = check_duplicate_local(contact, phone_map, email_map, google_id_map)
                    if status == "existing" and record_id:
                        airtable_client.patch_record(record_id, {
                            "Sync Lock": True,
                            "All Phones Raw": contact.get("all_phones_raw") or ""
                        })
                    else:
                        fields = build_airtable_fields(contact, account)
                        fields["Sync Lock"] = True
                        result = airtable_client.create_record(fields)
                        new_id = result.get("id")
                        if new_id and contact.get("dedup_phone"):
                            phone_map[contact["dedup_phone"]] = {"id": new_id, "fields": fields}
                stats["skipped"] += 1
                continue

            status, record_id, match_type = check_duplicate_local(contact, phone_map, email_map, google_id_map)
            fields = build_airtable_fields(contact, account)

            if status == "new":
                if dry_run:
                    logger.info(f"[DRY RUN] Would CREATE: {contact.get('full_name')} | {contact.get('dedup_phone')}")
                else:
                    result = airtable_client.create_record(fields)
                    new_id = result.get("id")
                    if new_id:
                        # Invalidate cache so Direction 2 sees fresh data
                        if hasattr(airtable_client, '_records_cache'):
                            del airtable_client._records_cache
                        # Record what we just wrote so watcher won't re-trigger
                        record_sync_write(new_id, {
                            "First Name": fields.get("First Name", ""),
                            "Last Name":  fields.get("Last Name", ""),
                            "Dedup Phone": fields.get("Dedup Phone", ""),
                            "Email":       fields.get("Clean Email", "")
                        })
                        fake_record = {"id": new_id, "fields": fields}
                        for p in [contact.get("dedup_phone"), contact.get("phone2"), contact.get("phone3")]:
                            if p:
                                phone_map[p] = fake_record
                        if contact.get("clean_email"):
                            email_map[contact["clean_email"]] = fake_record
                        upsert_dedup_cache(
                            contact["dedup_phone"],
                            contact.get("clean_email"),
                            new_id,
                            contact.get("google_contact_id")
                        )
                    logger.info(f"CREATED: {contact.get('full_name')} | {contact.get('dedup_phone')}")
                stats["created"] += 1

            elif status == "existing":
                if dry_run:
                    logger.info(f"[DRY RUN] Would UPDATE {record_id}: {contact.get('full_name')}")
                else:
                    existing_record = (
                        phone_map.get(contact.get("dedup_phone", "")) or
                        email_map.get(contact.get("clean_email", "")) or {}
                    )
                    existing_fields = existing_record.get("fields", {})

                    if fields_have_changed(existing_fields, fields):
                        # Build minimal patch — only fields that actually differ
                        patch_fields = build_minimal_patch(existing_fields, fields, record_id=record_id)
                        airtable_client.patch_record(record_id, patch_fields)
                        # Invalidate cache so Direction 2 sees fresh data
                        if hasattr(airtable_client, '_records_cache'):
                            del airtable_client._records_cache
                        # Record what we just wrote so watcher won't re-trigger.
                        # IMPORTANT: only update sync_writes for fields the script
                        # ACTUALLY wrote (those in patch_fields). Fields that were
                        # skipped by build_minimal_patch (D1 SKIP — user edits we
                        # preserved) must keep their OLD sync_writes value so the
                        # watcher still sees them as pending user edits to push.
                        prior = get_last_sync_writes(record_id) or {}
                        updates = {}
                        # Watcher fields: First Name, Last Name, Dedup Phone, Email
                        # For each, use patched value if patched, else keep prior (or current Airtable if no prior)
                        for field, getter in [
                            ("First Name", lambda: existing_fields.get("First Name", "")),
                            ("Last Name",  lambda: existing_fields.get("Last Name", "")),
                        ]:
                            if field in patch_fields:
                                updates[field] = patch_fields[field]
                            else:
                                # Field was skipped (user edit) — keep prior sync_writes value
                                # so watcher still detects the pending push
                                if field in prior:
                                    updates[field] = prior[field]
                                # else leave it absent; record_sync_write will not touch it
                        # Dedup Phone — Direction 1 never patches this, but track current Airtable value
                        updates["Dedup Phone"] = existing_fields.get("Dedup Phone", "")
                        # Email — Direction 1 patches Clean Email, but watcher tracks "Email"
                        if "Clean Email" in patch_fields:
                            updates["Email"] = patch_fields["Clean Email"]
                        elif "Email" in prior:
                            updates["Email"] = prior["Email"]
                        else:
                            updates["Email"] = existing_fields.get("Email", "") or existing_fields.get("Clean Email", "")
                        record_sync_write(record_id, updates)
                        if contact.get("dedup_phone"):
                            upsert_dedup_cache(
                                contact["dedup_phone"],
                                contact.get("clean_email"),
                                record_id,
                                contact.get("google_contact_id")
                            )
                        logger.info(f"UPDATED {record_id}: {contact.get('full_name')}")
                    else:
                        logger.debug(f"No changes: {contact.get('full_name')}")
                stats["updated"] += 1

            elif status == "flagged":
                logger.warning(
                    f"FLAGGED duplicate: {contact.get('full_name')} | "
                    f"{contact.get('dedup_phone')} | match: {match_type}"
                )
                if not dry_run:
                    try:
                        airtable_client.patch_record(record_id, {"Sync Lock": True})
                        flag_duplicate(
                            contact.get("dedup_phone"),
                            contact.get("clean_email"),
                            contact.get("full_name"),
                            record_id, match_type
                        )
                    except Exception:
                        pass
                stats["skipped"] += 1

        except Exception as e:
            logger.error(f"Error processing {raw.get('resourceName', '?')}: {e}")
            stats["errors"] += 1

    return stats
