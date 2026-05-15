"""
Delete Engine — soft-delete bi-directional sync.

NEW BEHAVIOR (soft-delete with 30-day grace):
================================================================
When the script detects a record was deleted on ONE side:

  Direction A — Deleted in Airtable (Airtable record GONE):
    - Don't delete Google immediately
    - Save (google_id + contact data) in pending_deletions table
    - After 30 days, if still pending and not canceled, delete from Google.
    - User has no Airtable visibility for this case (just SQLite tracking).

  Direction B — Deleted in Google (Airtable record EXISTS):
    - Don't delete Airtable record
    - Set Skip Delete = TRUE on the Airtable record
    - Set Pending Deletion Since = today (Airtable date field)
    - Track in pending_deletions table for symmetry
    - After 30 days, if Skip Delete is STILL TRUE, delete from Airtable.
    - If Skip Delete becomes FALSE (user unchecked) → cancel pending deletion.

Safety:
  - 5% global threshold: if more than 5% of known records are detected as
    deleted in a single run, abort entirely.
  - Skip Delete = TRUE records are exempt from immediate detection
    (they're already in soft-delete tracking).
"""
from datetime import datetime, timezone, timedelta
import sqlite3
import json as _json
from logger import get_logger
from state_manager import (
    DB_PATH,
    get_known_record_pairs,
    upsert_known_records,
    delete_known_record
)
from google_writer import delete_contact

logger = get_logger()

# Safety threshold — abort delete sync if more than this % flagged in one run
SAFETY_THRESHOLD_PERCENT = 5

# Grace period — how many days to wait before actually deleting
GRACE_PERIOD_DAYS = 30


# =====================================================================
# Pending deletions DB helpers (local — keeps delete_engine self-contained)
# =====================================================================

def _record_pending(direction, airtable_id, google_id, contact_data=None):
    """Track a soft-delete in SQLite."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pid = f"{direction}|{airtable_id or ''}|{google_id or ''}"
    now = datetime.now(timezone.utc).isoformat()
    data_blob = _json.dumps(contact_data) if contact_data else ''
    c.execute(
        "INSERT OR REPLACE INTO pending_deletions "
        "(id, direction, airtable_id, google_id, detected_at, contact_data, canceled) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (pid, direction, airtable_id, google_id, now, data_blob)
    )
    conn.commit()
    conn.close()


def _get_active_pending():
    """Return all non-canceled pending deletions older than GRACE_PERIOD_DAYS."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS)).isoformat()
    c.execute(
        "SELECT id, direction, airtable_id, google_id, detected_at "
        "FROM pending_deletions "
        "WHERE canceled=0 AND detected_at < ?",
        (cutoff,)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def _get_all_pending_ids():
    """Return set of (direction, airtable_id, google_id) for ALL active pending."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT direction, airtable_id, google_id FROM pending_deletions WHERE canceled=0"
    )
    rows = c.fetchall()
    conn.close()
    return {(r[0], r[1] or '', r[2] or '') for r in rows}


def _cancel_pending(airtable_id):
    """User unchecked Skip Delete — cancel any pending deletion for this record."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE pending_deletions SET canceled=1 WHERE airtable_id=? AND canceled=0",
        (airtable_id,)
    )
    conn.commit()
    conn.close()


def _remove_pending(pid):
    """Remove a pending deletion record (after it's been actioned)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM pending_deletions WHERE id=?", (pid,))
    conn.commit()
    conn.close()


# =====================================================================
# Main delete sync
# =====================================================================

def run_delete_sync(airtable_client, service, dry_run=False):
    stats = {
        "deleted_from_google":  0,  # actually deleted (after grace period)
        "deleted_from_airtable": 0,  # actually deleted (after grace period)
        "soft_flagged_airtable": 0,  # marked Skip Delete + Pending Deletion Since
        "soft_flagged_google":   0,  # tracked in DB only (Airtable record gone)
        "canceled":              0,  # user unchecked Skip Delete
        "aborted_by_safety":     0,
        "errors":                0
    }

    # --- 1) Fetch current state ---
    logger.info("[DELETE SYNC] Fetching current Airtable records...")
    try:
        current_airtable = airtable_client.get_all_records()
    except Exception as e:
        logger.error(f"[DELETE SYNC] Failed to load Airtable: {e}")
        return stats

    current_at_ids   = set()
    current_pairs    = {}
    skip_delete_ids  = set()
    pending_dates    = {}   # airtable_id -> Pending Deletion Since (ISO date)
    airtable_records = {}   # airtable_id -> full record (for quick access)

    for record in current_airtable:
        rid    = record["id"]
        fields = record.get("fields", {})
        gid    = (fields.get("Google Contact ID") or "").strip()

        airtable_records[rid] = record
        if fields.get("Skip Delete"):
            skip_delete_ids.add(rid)
        if fields.get("Pending Deletion Since"):
            pending_dates[rid] = fields["Pending Deletion Since"]
        if gid:
            key = f"{rid}|{gid}"
            current_at_ids.add(rid)
            current_pairs[key] = (rid, gid)

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

    known_pairs = get_known_record_pairs("pair")
    logger.info(
        f"[DELETE SYNC] Known: {len(known_pairs)} | "
        f"Airtable: {len(current_at_ids)} | "
        f"Google: {len(current_google_ids)}"
    )

    # --- 2) Cancel any pending deletions whose Skip Delete was unchecked ---
    pending_set = _get_all_pending_ids()  # {(direction, aid, gid)}
    for direction, aid, gid in pending_set:
        if direction == 'airtable_kept' and aid:
            # If Skip Delete is no longer TRUE, user canceled
            rec = airtable_records.get(aid)
            if rec is not None:
                still_skip = rec.get('fields', {}).get('Skip Delete')
                if not still_skip:
                    _cancel_pending(aid)
                    stats['canceled'] += 1
                    logger.info(f"[DELETE SYNC] User unchecked Skip Delete on {aid} — pending deletion canceled")

    # --- 3) Detect new soft-delete events ---
    soft_flag_b = []  # (known_key, aid, gid) — Direction B (Google deleted, mark Airtable)
    soft_flag_a = []  # (known_key, aid, gid) — Direction A (Airtable deleted, track DB only)

    pending_lookup = {(d, a, g) for (d, a, g) in pending_set}

    for known_key, (aid, gid) in known_pairs.items():
        if not aid or not gid:
            continue

        # Skip Delete = TRUE → already in soft-delete tracking, skip detection
        if aid in skip_delete_ids:
            continue

        in_airtable = aid in current_at_ids
        in_google   = gid in current_google_ids

        # Both gone: nothing to do — clean up known record
        if not in_airtable and not in_google:
            if not dry_run:
                delete_known_record(known_key, "pair")
            continue

        # Direction B — deleted in Google, Airtable still has it
        if in_airtable and not in_google:
            if ('airtable_kept', aid, gid) in pending_lookup:
                continue  # already tracked
            soft_flag_b.append((known_key, aid, gid))

        # Direction A — deleted in Airtable, Google still has it
        elif not in_airtable and in_google:
            if ('google_kept', aid, gid) in pending_lookup:
                continue  # already tracked
            soft_flag_a.append((known_key, aid, gid))

    total_new_soft = len(soft_flag_a) + len(soft_flag_b)

    # --- 4) Safety threshold check ---
    total_known = len(known_pairs) if known_pairs else 1
    percent     = (total_new_soft / total_known) * 100.0
    if total_new_soft > 0 and percent > SAFETY_THRESHOLD_PERCENT:
        logger.error(
            f"[DELETE SYNC] *** SAFETY ABORT *** "
            f"Detected {total_new_soft} soft-delete candidates "
            f"({percent:.2f}% of {total_known}) — exceeds {SAFETY_THRESHOLD_PERCENT}% threshold. "
            f"Skipping ALL deletions this run. Manual review required."
        )
        # Save list to CSV for review
        try:
            import csv as _csv
            ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
            review_path = f"/root/contact-sync/data/safety_aborted_{ts}.csv"
            with open(review_path, "w", encoding="utf-8", newline="") as _f:
                _w = _csv.writer(_f)
                _w.writerow(["Direction", "AirtableID", "GoogleID"])
                for k, a, g in soft_flag_a:
                    _w.writerow(["A: deleted-in-airtable", a, g])
                for k, a, g in soft_flag_b:
                    _w.writerow(["B: deleted-in-google", a, g])
            logger.error(f"[DELETE SYNC] Aborted plan saved to: {review_path}")
        except Exception as _e:
            logger.error(f"[DELETE SYNC] Could not save abort report: {_e}")

        stats["aborted_by_safety"] = total_new_soft
        # Still refresh known_records to reflect current state
        if not dry_run:
            new_pairs = [(k, a, g) for k, (a, g) in current_pairs.items()]
            upsert_known_records("pair", new_pairs)
        return stats

    # --- 5) Apply Direction B soft-flags (mark Airtable records) ---
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for known_key, aid, gid in soft_flag_b:
        logger.info(f"[DELETE SYNC] SOFT-FLAG (Google deleted): Airtable {aid} -> mark Skip Delete + Pending Deletion Since={today_iso}")
        if dry_run:
            stats["soft_flagged_airtable"] += 1
            continue
        try:
            airtable_client.patch_record(aid, {
                "Skip Delete": True,
                "Pending Deletion Since": today_iso
            })
            _record_pending('airtable_kept', aid, gid)
            stats["soft_flagged_airtable"] += 1
        except Exception as e:
            logger.error(f"[DELETE SYNC] Failed to soft-flag Airtable {aid}: {e}")
            stats["errors"] += 1

    # --- 6) Apply Direction A soft-flags (DB-only tracking, no Airtable record) ---
    for known_key, aid, gid in soft_flag_a:
        logger.info(f"[DELETE SYNC] SOFT-FLAG (Airtable deleted): Google {gid} -> tracked in DB, will delete after {GRACE_PERIOD_DAYS} days")
        if dry_run:
            stats["soft_flagged_google"] += 1
            continue
        _record_pending('google_kept', aid, gid)
        stats["soft_flagged_google"] += 1

    # --- 7) Process pending deletions whose grace period has expired ---
    expired = _get_active_pending()
    if expired:
        logger.info(f"[DELETE SYNC] Found {len(expired)} pending deletions past {GRACE_PERIOD_DAYS}-day grace period")

    for pid, direction, aid, gid, detected_at in expired:
        if direction == 'airtable_kept':
            # User had 30 days to uncheck. They didn't. Delete from Airtable.
            # First, double-check Skip Delete is still TRUE
            rec = airtable_records.get(aid)
            if rec is None:
                # Already gone somehow — clean up
                _remove_pending(pid)
                continue
            still_flagged = rec.get('fields', {}).get('Skip Delete')
            if not still_flagged:
                # User unchecked at some point — cancel
                _cancel_pending(aid)
                stats['canceled'] += 1
                continue

            logger.info(f"[DELETE SYNC] GRACE EXPIRED — deleting Airtable {aid}")
            if dry_run:
                stats["deleted_from_airtable"] += 1
                continue
            try:
                airtable_client.delete_record(aid)
                _remove_pending(pid)
                delete_known_record(f"{aid}|{gid}", "pair")
                stats["deleted_from_airtable"] += 1
            except Exception as e:
                logger.error(f"[DELETE SYNC] Failed to delete expired Airtable {aid}: {e}")
                stats["errors"] += 1

        elif direction == 'google_kept':
            # 30 days passed — delete from Google
            logger.info(f"[DELETE SYNC] GRACE EXPIRED — deleting Google {gid}")
            if dry_run:
                stats["deleted_from_google"] += 1
                continue
            try:
                ok = delete_contact(service, gid)
                if ok:
                    _remove_pending(pid)
                    delete_known_record(f"{aid}|{gid}", "pair")
                    stats["deleted_from_google"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.error(f"[DELETE SYNC] Failed to delete expired Google {gid}: {e}")
                stats["errors"] += 1

    # --- 8) Refresh known_records ---
    if not dry_run:
        new_pairs = [(k, a, g) for k, (a, g) in current_pairs.items()]
        upsert_known_records("pair", new_pairs)
        logger.info(f"[DELETE SYNC] Refreshed known_records: {len(new_pairs)} entries")

    logger.info(
        f"[DELETE SYNC] Done: "
        f"soft_flagged_airtable={stats['soft_flagged_airtable']} | "
        f"soft_flagged_google={stats['soft_flagged_google']} | "
        f"deleted_from_airtable={stats['deleted_from_airtable']} | "
        f"deleted_from_google={stats['deleted_from_google']} | "
        f"canceled={stats['canceled']} | "
        f"errors={stats['errors']}"
    )
    return stats
