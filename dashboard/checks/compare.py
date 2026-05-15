"""compare.py — Airtable vs Google comparison, read from cache.db.

The cache is populated every 5 min by workers/update_counts.py.
This module is fast (just a SQLite read).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

CACHE_DB = Path('/root/contact-sync-dashboard/data/cache.db')


def _local_tz():
    return datetime.now().astimezone().tzinfo


def get_comparison():
    if not CACHE_DB.exists():
        return {
            'available': False,
            'message':   'Cache database not yet created — first updater run hasn\'t completed',
        }
    try:
        conn = sqlite3.connect(f'file:{CACHE_DB}?mode=ro', uri=True)
        c = conn.cursor()
        c.execute("SELECT key, value, updated_at FROM metrics")
        rows = c.fetchall()
        # Last successful update run
        c.execute("""
            SELECT started_at, finished_at, status, error
              FROM update_log
             ORDER BY id DESC
             LIMIT 1
        """)
        last_run = c.fetchone()
        conn.close()
    except Exception as e:
        return {'available': False, 'message': f'Cache read error: {e}'}

    if not rows:
        return {'available': False, 'message': 'No data in cache yet'}

    metrics = {k: v for k, v in [(r[0], r[1]) for r in rows]}
    updated_at_iso = rows[0][2] if rows else None

    # Compute freshness
    age_minutes = None
    if updated_at_iso:
        try:
            dt = datetime.fromisoformat(updated_at_iso)
            age_minutes = round((datetime.now(dt.tzinfo) - dt).total_seconds() / 60, 1)
        except Exception:
            pass

    def _int(k, default=0):
        try:
            return int(metrics.get(k, default))
        except Exception:
            return default

    airtable_count       = _int('airtable_count')
    google_count         = _int('google_count')
    matched_pairs        = _int('matched_pairs')
    airtable_only_phones = _int('airtable_only_phones')
    google_only_phones   = _int('google_only_phones')
    mismatch_count       = _int('mismatch_count')
    pending_deletion     = _int('pending_deletion_count')
    duplicates           = _int('duplicates_in_airtable')

    diff_count = abs(airtable_count - google_count)

    last_update = {}
    if last_run:
        started, finished, status, error = last_run
        last_update = {
            'started_at':  started,
            'finished_at': finished,
            'status':      status,
            'error':       (error[:200] if error else None),
        }

    return {
        'available':              True,
        'airtable_count':         airtable_count,
        'google_count':           google_count,
        'difference':             diff_count,
        'matched_pairs':          matched_pairs,
        'airtable_only_phones':   airtable_only_phones,
        'google_only_phones':     google_only_phones,
        'mismatch_count':         mismatch_count,
        'pending_deletion_count': pending_deletion,
        'duplicates_in_airtable': duplicates,
        'updated_at':             updated_at_iso,
        'age_minutes':            age_minutes,
        'last_update_run':        last_update,
    }


if __name__ == '__main__':
    import json as _j
    print(_j.dumps(get_comparison(), indent=2, default=str))
