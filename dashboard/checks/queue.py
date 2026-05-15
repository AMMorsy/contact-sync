"""queue.py — live state of locks, running processes, and the push queue size.

Reads:
- File-locks under /root/contact-sync/data/*.lock
- Running processes via `ps`
- Push queue: counts records the watcher considers user-edited (needs to import the sync codebase)
"""
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone

LOCK_DIR = Path('/root/contact-sync/data')
LOCK_FILES = {
    'push':   LOCK_DIR / 'push.lock',
    'pull':   LOCK_DIR / 'pull.lock',
    'delete': LOCK_DIR / 'delete.lock',
    'full':   LOCK_DIR / 'sync.lock',     # daily 4 AM main.py
}

PROCESS_LABELS = {
    'run_push.py':   'push',
    'run_pull.py':   'pull',
    'run_delete.py': 'delete',
    'main.py':       'full',
}


def get_locks():
    """For each lock file, report whether it exists, its mtime, and if a process holds it."""
    out = {}
    now = time.time()
    for label, path in LOCK_FILES.items():
        info = {'exists': path.exists()}
        if info['exists']:
            mtime = path.stat().st_mtime
            info['age_seconds'] = round(now - mtime, 1)
            info['mtime']       = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            info['held_by_running_process'] = False    # filled below
        out[label] = info
    return out


def get_running_processes():
    """List sync-system processes currently running."""
    try:
        result = subprocess.run(
            ['ps', '-eo', 'pid,etimes,cmd'],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return []
    running = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3: continue
        pid, etimes, cmd = parts
        try:
            etimes = int(etimes)
        except ValueError:
            continue
        for marker, label in PROCESS_LABELS.items():
            if marker in cmd and 'python' in cmd:
                running.append({
                    'kind':    label,
                    'pid':     int(pid),
                    'elapsed_seconds': etimes,
                    'cmd':     cmd[:200],
                })
                break
    return running


def get_push_queue_size():
    """Ask the watcher how many records currently look like user edits."""
    try:
        # Import sync codebase lazily to avoid loading it for every dashboard hit
        sys.path.insert(0, '/root/contact-sync/src')
        from airtable_api import AirtableClient
        from airtable_watcher import get_records_to_push
        import json
        cfg = json.load(open('/root/contact-sync/config/settings.json'))
        airtable = AirtableClient(
            cfg['airtable']['token'], cfg['airtable']['base_id'], cfg['airtable']['table_id']
        )
        records = get_records_to_push(airtable)
        return {'available': True, 'count': len(records)}
    except Exception as e:
        return {'available': False, 'error': str(e)[:200]}


def get_state():
    locks = get_locks()
    procs = get_running_processes()
    # Annotate each lock with whether a process is actually running for it
    for p in procs:
        kind = p['kind']
        if kind in locks and locks[kind].get('exists'):
            locks[kind]['held_by_running_process'] = True
    return {
        'locks':     locks,
        'processes': procs,
    }


if __name__ == '__main__':
    import json as _j
    out = get_state()
    # Note: push queue is expensive (~2 min Airtable load) — only run on demand
    if '--queue' in sys.argv:
        out['push_queue'] = get_push_queue_size()
    print(_j.dumps(out, indent=2, default=str))
