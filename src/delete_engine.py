"""
Delete Engine — handles bi-directional deletion sync.

Logic:
  1. On every run, fetch current Airtable records and Google contacts.
  2. Compare to known_records table (last seen list).
  3. If a record was in known_records but is NOT in current Airtable
     → user deleted it in Airtable → delete from Google.
  4. If a record was in known_records but is NOT in current Google
     → user deleted it on phone/Google → delete from Airtable.
  5. Update known_records to reflect current state.

Safety:
  - Only operates on records that have BOTH an Airtable ID AND a Google ID
    (i.e., were previously synced — not new records).
  - Uses the SAME pair (airtable_id, google_id) as the unique key.
"""
from logger import get_logger
from state_manager import (
    get_known_record_pairs,
    upsert_known_records,
    delete_known_record
)
from google_writer import delete_contact

logger = get_logger()


def run_delete_sync(airtable_client, service, dry_run=False):
    """
    Detect and sync deletions in both directions.
    Must be called AFTER Direction 1 (Google → Airtable) and BEFORE
    Direction 2 (Airtable → Google), so we have current state of both.
    """
    stats = {
        "deleted_from_google": 0,
        "deleted_from_airtable": 0,
        "errors": 0
    }

    # 1) Fetch current Airtable records (only those with Google Contact ID)
    logger.info("[DELETE SYNC] Fetching current Airtable records...")
    try:
        current_airtable = airtable_client.get_all_records()
    except Exception as e:
        logger.error(f"[DELETE SYNC] Failed to load Airtable: {e}")
        return stats

    current_at_ids = set()
    current_pairs = {}  # key: (airtable_id, google_id) — value: True
    skip_delete_ids = set()  # records flagged Skip Delete = true
    for record in current_airtable:
        rid = record["id"]
        fields = record.get("fields", {})
        gid = (fields.get("Google Contact ID") or "").strip()
        if fields.get("Skip Delete"):
            skip_delete_ids.add(rid)
        if gid:
            key = f"{rid}|{gid}"
            current_at_ids.add(rid)
            current_pairs[key] = (rid, gid)

    # 2) Fetch current Google contacts
    logger.info("[DELETE SYNC] Fetching current Google contacts...")
    current_google_ids = set()
    try:
        page_token = None
        while True:
            kwargs = {
                'resourceName': 'people/me',
                'pageSize': 1000,
                'personFields': 'metadata'
            }
            if page_token:
                kwargs['pageToken'] = page_token
            resp = service.people().connections().list(**kwargs).execute()
            for c in resp.get("connections", []):
                rname = c.get("resourceName", "")
                if rname:
                    current_google_ids.add(rname)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logger.error(f"[DELETE SYNC] Failed to load Google contacts: {e}")
        return stats

    # 3) Get last-known pairs from our database
    known_pairs = get_known_record_pairs("pair")
    logger.info(
        f"[DELETE SYNC] Known: {len(known_pairs)} | "
        f"Airtable: {len(current_at_ids)} | "
        f"Google: {len(current_google_ids)}"
    )

    # 4) Detect deletions
    deletions_at_to_google = []   # list of google_ids to delete from Google
    deletions_google_to_at = []   # list of airtable_ids to delete from Airtable

    for known_key, (aid, gid) in known_pairs.items():
        if not aid or not gid:
            continue
        in_airtable = aid in current_at_ids
        in_google   = gid in current_google_ids

        # Skip records flagged with Skip Delete = true
        if aid in skip_delete_ids:
            continue

        if not in_airtable and in_google:
            # Deleted from Airtable → must delete from Google
            deletions_at_to_google.append((known_key, aid, gid))

        elif in_airtable and not in_google:
            # Deleted from Google → must delete from Airtable
            deletions_google_to_at.append((known_key, aid, gid))

        # If neither exists, also remove from known table
        elif not in_airtable and not in_google:
            if not dry_run:
                delete_known_record(known_key, "pair")

    # 5) Execute Airtable→Google deletes
    for known_key, aid, gid in deletions_at_to_google:
        logger.info(f"[DELETE SYNC] Deleting from Google (was in Airtable): {gid}")
        if dry_run:
            logger.info("[DRY RUN] Would delete from Google")
            stats["deleted_from_google"] += 1
            continue
        success = delete_contact(service, gid)
        if success:
            delete_known_record(known_key, "pair")
            stats["deleted_from_google"] += 1
        else:
            stats["errors"] += 1

    # 6) Execute Google→Airtable deletes
    for known_key, aid, gid in deletions_google_to_at:
        logger.info(f"[DELETE SYNC] Deleting from Airtable (was in Google): {aid}")
        if dry_run:
            logger.info("[DRY RUN] Would delete from Airtable")
            stats["deleted_from_airtable"] += 1
            continue
        try:
            airtable_client.delete_record(aid)
            delete_known_record(known_key, "pair")
            stats["deleted_from_airtable"] += 1
        except Exception as e:
            logger.error(f"[DELETE SYNC] Failed to delete Airtable {aid}: {e}")
            stats["errors"] += 1

    # 7) Refresh known_records with current state
    if not dry_run:
        new_pairs = []
        for key, (aid, gid) in current_pairs.items():
            new_pairs.append((key, aid, gid))
        upsert_known_records("pair", new_pairs)
        logger.info(f"[DELETE SYNC] Refreshed known_records: {len(new_pairs)} entries")

    logger.info(
        f"[DELETE SYNC] Done: "
        f"Google deletions={stats['deleted_from_google']} | "
        f"Airtable deletions={stats['deleted_from_airtable']} | "
        f"errors={stats['errors']}"
    )
    return stats
