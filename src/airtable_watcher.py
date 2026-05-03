"""
Airtable Watcher — detects changes that need pushing to Google.

Strategy: Compare current Airtable field values against the last values
the sync script wrote (stored in SQLite). If any watched field differs,
it's a user edit and needs to be pushed to Google.

This bypasses Airtable's auto-updated 'Updated At' timestamp entirely,
so it cannot be triggered by the sync script's own writes.
"""
from logger import get_logger
from state_manager import is_user_edit

logger = get_logger()

# Only these 4 fields trigger a push to Google
WATCHED_FIELDS = ["First Name", "Last Name", "Dedup Phone", "Email"]


def get_records_to_push(airtable_client):
    """Find Airtable records edited by the user that need pushing to Google.
    Uses the cached get_all_records() so we don't re-fetch from Airtable."""
    try:
        all_records = airtable_client.get_all_records()
    except Exception as e:
        logger.error(f"Airtable watcher fetch failed: {e}")
        return []

    # Filter in memory to candidate records
    candidates = []
    for record in all_records:
        fields = record.get("fields", {})
        if fields.get("Sync Lock"):
            continue
        gid = (fields.get("Google Contact ID") or "").strip()
        if not gid:
            continue
        first = (fields.get("First Name") or "").strip()
        phone = (fields.get("Dedup Phone") or "").strip()
        if not first or not phone:
            continue
        candidates.append(record)

    # Check user edits via SQLite tracking
    edited = []
    for record in candidates:
        fields = record.get("fields", {})
        comparable = {
            "First Name":  fields.get("First Name", ""),
            "Last Name":   fields.get("Last Name", ""),
            "Dedup Phone": fields.get("Dedup Phone", ""),
            "Email":       fields.get("Email") or fields.get("Clean Email", "")
        }
        if is_user_edit(record["id"], comparable, WATCHED_FIELDS):
            edited.append(record)

    logger.info(
        f"Airtable watcher: scanned {len(candidates)} eligible records "
        f"(of {len(all_records)} total), {len(edited)} have user edits to push"
    )
    return edited


def get_all_unpushed_records(airtable_client):
    """Records with NO Google Contact ID yet — uses the cached get_all_records()."""
    try:
        all_records = airtable_client.get_all_records()
    except Exception as e:
        logger.error(f"Failed to get unpushed records: {e}")
        return []

    unpushed = []
    for record in all_records:
        fields = record.get("fields", {})
        if fields.get("Sync Lock"):
            continue
        gid = (fields.get("Google Contact ID") or "").strip()
        if gid:
            continue
        first = (fields.get("First Name") or "").strip()
        phone = (fields.get("Dedup Phone") or "").strip()
        if not first or not phone:
            continue
        unpushed.append(record)

    logger.info(f"Found {len(unpushed)} unpushed records (no Google Contact ID)")
    return unpushed
