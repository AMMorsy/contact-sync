"""sync_status.py — read the latest sync activity from the live system.

Returns a dict with the most-recent run for each sync direction:
  pull   (Direction 1 — Google → Airtable)   from SQLite run_log
  push   (Direction 2 — Airtable → Google)   from cron-push.log
  delete (Soft-delete detection)             from cron-delete.log
  full   (Daily safety net main.py)          from cron.log
"""
import re
import sqlite3
import ast
from datetime import datetime, timezone
from pathlib import Path

SYNC_DB = '/root/contact-sync/data/sync_state.db'
LOG_DIR = Path('/root/contact-sync/logs')

# Match: "2026-05-13 00:14:01 | INFO | <Marker>: {<stats dict>}"
_TS_RE   = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'
_PUSH_RE   = re.compile(_TS_RE + r' \| INFO \| Push run: (\{.*\})')
_DELETE_RE = re.compile(_TS_RE + r' \| INFO \| Delete sync done: (\{.*\})')


def _iso(s):
    """Parse 'YYYY-MM-DD HH:MM:SS' as UTC ISO string."""
    try:
        dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _tail_match(path, regex, max_bytes=1_000_000):
    """Read last N bytes of a log file, return last regex match (most recent run)."""
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with path.open('rb') as f:
            f.seek(max(0, size - max_bytes))
            text = f.read().decode('utf-8', errors='ignore')
        matches = list(regex.finditer(text))
        if not matches:
            return None
        return matches[-1]
    except Exception:
        return None


def get_last_pull():
    """Latest completed pull from SQLite run_log."""
    try:
        conn = sqlite3.connect(SYNC_DB)
        c = conn.cursor()
        c.execute("""
            SELECT started_at, finished_at, contacts_pulled, contacts_created,
                   contacts_updated, contacts_skipped, errors, status
            FROM run_log
            WHERE status IN ('done', 'error')
            ORDER BY id DESC LIMIT 1
        """)
        row = c.fetchone()
        # Also check if anything is currently running
        c.execute("SELECT started_at FROM run_log WHERE status='running' ORDER BY id DESC LIMIT 1")
        running = c.fetchone()
        conn.close()
        if not row:
            return {'available': False, 'running': bool(running)}
        started, finished, pulled, created, updated, skipped, errors, status = row
        # Duration
        duration_sec = None
        try:
            if started and finished:
                ds = datetime.fromisoformat(started)
                df = datetime.fromisoformat(finished)
                duration_sec = (df - ds).total_seconds()
        except Exception:
            pass
        return {
            'available':   True,
            'started_at':  started,
            'finished_at': finished,
            'duration_seconds': duration_sec,
            'pulled':   pulled,
            'created':  created,
            'updated':  updated,
            'skipped':  skipped,
            'errors':   errors,
            'status':   status,
            'running':  bool(running),
        }
    except Exception as e:
        return {'available': False, 'error': str(e)}


def _parse_log_match(match):
    if not match:
        return None
    ts, raw_stats = match.group(1), match.group(2)
    try:
        stats = ast.literal_eval(raw_stats)
    except Exception:
        stats = {}
    return {
        'available':   True,
        'finished_at': _iso(ts),
        'stats':       stats,
    }


def get_last_push():
    """Latest push run from cron-push.log."""
    m = _tail_match(LOG_DIR / 'cron-push.log', _PUSH_RE)
    result = _parse_log_match(m)
    if not result:
        return {'available': False}
    s = result['stats']
    result['checked']  = s.get('checked', 0)
    result['created']  = s.get('created', 0)
    result['updated']  = s.get('updated', 0)
    result['skipped']  = s.get('skipped', 0)
    result['errors']   = s.get('errors', 0)
    result['status']   = 'ok' if result['errors'] == 0 else 'has_errors'
    return result


def get_last_delete():
    """Latest delete-sync run from cron-delete.log."""
    m = _tail_match(LOG_DIR / 'cron-delete.log', _DELETE_RE)
    result = _parse_log_match(m)
    if not result:
        return {'available': False}
    s = result['stats']
    result['deleted_from_google']   = s.get('deleted_from_google', 0)
    result['deleted_from_airtable'] = s.get('deleted_from_airtable', 0)
    result['soft_flagged_airtable'] = s.get('soft_flagged_airtable', 0)
    result['soft_flagged_google']   = s.get('soft_flagged_google', 0)
    result['canceled']              = s.get('canceled', 0)
    result['aborted_by_safety']     = s.get('aborted_by_safety', 0)
    result['errors']                = s.get('errors', 0)
    result['status']  = 'aborted' if result['aborted_by_safety'] else ('ok' if result['errors'] == 0 else 'has_errors')
    return result


def get_status_all():
    """Aggregate everything for the dashboard."""
    return {
        'pull':   get_last_pull(),
        'push':   get_last_push(),
        'delete': get_last_delete(),
    }


if __name__ == '__main__':
    import json as _j
    print(_j.dumps(get_status_all(), indent=2, default=str))
