from datetime import datetime, timezone, timedelta
from logger import get_logger
from airtable_watcher import get_records_to_push, get_all_unpushed_records
from google_writer import create_contact, update_contact, build_service
from auth import get_credentials
from state_manager import record_sync_write

logger = get_logger()

# Conflict tolerance: if Airtable Updated At is within X minutes of Google's
# update time, Google wins (per client's design choice).
CONFLICT_TOLERANCE_MINUTES = 5

# Loop prevention: stamp Last Synced At this many seconds AFTER now so that
# the auto-updated Airtable "Updated At" is BEFORE Last Synced At.
SYNC_STAMP_FUTURE_SECONDS = 60


def _parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except Exception:
        return None


def _get_google_update_time(service, resource_name):
    try:
        contact = service.people().get(
            resourceName=resource_name,
            personFields="metadata"
        ).execute()
        sources = contact.get("metadata", {}).get("sources", [])
        latest = None
        for src in sources:
            t = _parse_iso(src.get("updateTime"))
            if t and (latest is None or t > latest):
                latest = t
        return latest
    except Exception as e:
        logger.warning(f"Could not fetch Google update time for {resource_name}: {e}")
        return None


def stamp_airtable_record(airtable_client, record_id, google_contact_id=None, source_fields=None):
    """Stamp Last Synced At and record the field values we just sent to Google.
    The recorded values let the watcher distinguish script-writes from user-edits.

    source_fields: the Airtable fields dict that was just synced — used to record
                   what values are now live in Google."""
    future = datetime.now(timezone.utc) + timedelta(seconds=SYNC_STAMP_FUTURE_SECONDS)
    fields = {
        "Last Synced At": future.isoformat(),
        "Sync Source": "Google Account 1",
        "Sync Lock": False
    }
    if google_contact_id:
        fields["Google Contact ID"] = google_contact_id
    try:
        airtable_client.patch_record(record_id, fields)
    except Exception as e:
        logger.error(f"Failed to stamp Airtable record {record_id}: {e}")
        return

    # Record what we just sent to Google — so watcher knows this is our write
    if source_fields is not None:
        try:
            record_sync_write(record_id, {
                "First Name":  source_fields.get("First Name", ""),
                "Last Name":   source_fields.get("Last Name", ""),
                "Dedup Phone": source_fields.get("Dedup Phone", ""),
                "Email":       source_fields.get("Email") or source_fields.get("Clean Email", "")
            })
        except Exception as e:
            logger.error(f"Failed to record sync_write for {record_id}: {e}")


def lock_record(airtable_client, record_id):
    try:
        airtable_client.patch_record(record_id, {"Sync Lock": True})
    except Exception as e:
        logger.error(f"Failed to lock record {record_id}: {e}")


def unlock_record(airtable_client, record_id):
    try:
        airtable_client.patch_record(record_id, {"Sync Lock": False})
    except Exception as e:
        logger.error(f"Failed to unlock record {record_id}: {e}")


def run_push(airtable_client, account, dry_run=False):
    stats = {"checked": 0, "created": 0, "updated": 0, "skipped": 0, "errors": 0}

    try:
        creds = get_credentials(account)
        service = build_service(creds)
    except Exception as e:
        logger.error(f"Cannot connect to Google for push: {e}")
        return stats

    records_to_push = get_records_to_push(airtable_client)
    unpushed = get_all_unpushed_records(airtable_client)

    seen = set()
    all_records = []
    for r in records_to_push + unpushed:
        if r["id"] not in seen:
            seen.add(r["id"])
            all_records.append(r)

    logger.info(f"Push engine: {len(all_records)} total records to process")

    for record in all_records:
        stats["checked"] += 1
        record_id = record["id"]
        fields = record.get("fields", {})

        # Mandatory fields
        first = (fields.get("First Name") or "").strip()
        phone = (fields.get("Dedup Phone") or "").strip()
        if not first or not phone:
            logger.debug(f"Skipping {record_id} — First Name and Dedup Phone mandatory")
            stats["skipped"] += 1
            continue

        google_contact_id = (fields.get("Google Contact ID") or "").strip()

        if not dry_run:
            lock_record(airtable_client, record_id)

        try:
            if google_contact_id:
                # Google-wins conflict resolution within tolerance
                google_update = _get_google_update_time(service, google_contact_id)
                airtable_update = _parse_iso(fields.get("Updated At"))

                if google_update and airtable_update:
                    diff = (airtable_update - google_update).total_seconds() / 60.0
                    if 0 < diff <= CONFLICT_TOLERANCE_MINUTES:
                        logger.info(
                            f"Google wins for {google_contact_id} "
                            f"(Airtable only {diff:.1f}min ahead) — skipping push"
                        )
                        if not dry_run:
                            stamp_airtable_record(airtable_client, record_id, source_fields=fields)
                        stats["skipped"] += 1
                        continue

                result = update_contact(service, google_contact_id, fields, dry_run=dry_run)

                if dry_run:
                    stats["updated"] += 1
                elif result == "not_found":
                    logger.warning(f"Recreating: {first}")
                    new_id = create_contact(service, fields, dry_run=False)
                    if new_id:
                        stamp_airtable_record(airtable_client, record_id, google_contact_id=new_id, source_fields=fields)
                        stats["created"] += 1
                    else:
                        unlock_record(airtable_client, record_id)
                        stats["errors"] += 1
                elif result:
                    stamp_airtable_record(airtable_client, record_id, source_fields=fields)
                    stats["updated"] += 1
                else:
                    unlock_record(airtable_client, record_id)
                    stats["errors"] += 1
            else:
                new_id = create_contact(service, fields, dry_run=dry_run)
                if dry_run:
                    stats["created"] += 1
                elif new_id:
                    stamp_airtable_record(airtable_client, record_id, google_contact_id=new_id, source_fields=fields)
                    stats["created"] += 1
                else:
                    unlock_record(airtable_client, record_id)
                    stats["errors"] += 1

        except Exception as e:
            logger.error(f"Push failed for {record_id}: {e}")
            if not dry_run:
                unlock_record(airtable_client, record_id)
            stats["errors"] += 1

    return stats
