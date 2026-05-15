"""backups.py — status of recent backup runs.

Checks:
  google_csv  — Daily Google contacts CSV (2 AM local)
  full_server — Full server backup (Mon & Thu, 3 AM local)
  synology    — Live SSH reachability test
  local_csv   — Most recent CSV file on local disk

Reports "ok" / "failed" / "stale" with human-readable detail.
"""
import re
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ----- Backup log files -----
GOOGLE_CSV_LOG = Path('/root/google_contacts_csv_backup.log')
FULL_BACKUP_LOG = Path('/root/full_backup.log')
LOCAL_CSV_DIR = Path('/root/contact-sync/data/csv_backup')

# Backup destination — set these in dashboard config.json
# (synology_user / synology_ip / synology_ssh_key)
import json as _json_for_synology_cfg
try:
    _cfg = _json_for_synology_cfg.load(open('config.json'))
except Exception:
    _cfg = {}
SYNOLOGY_USER = _cfg.get('synology_user', 'backup_user')
SYNOLOGY_IP   = _cfg.get('synology_ip',   '192.168.1.10')
SYNOLOGY_KEY  = _cfg.get('synology_ssh_key', '~/.ssh/backup_key')

# Log timestamp pattern: "[2026-05-14 02:00:01] ..."
_TS_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')

# Strong signals that a run completed successfully vs failed
SUCCESS_MARKERS = ('completed successfully', 'BACKUP COMPLETE', 'DONE',
                    'Backup completed', 'CSV written')
FAILURE_MARKERS = ('rsync error', 'connection unexpectedly closed',
                    'Permission denied', 'FAILED', 'Error')


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_log_tail(path: Path, tail_bytes=80_000):
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with path.open('rb') as f:
            f.seek(max(0, size - tail_bytes))
            return f.read().decode('utf-8', errors='ignore')
    except Exception:
        return None


def _split_runs(text: str):
    """Split the tail into individual run sections by 'STARTING' marker."""
    if not text:
        return []
    # Split on lines containing 'STARTING'
    starts = [m.start() for m in re.finditer(r'={3,} STARTING', text)]
    if not starts:
        return [text]
    sections = []
    for i, s in enumerate(starts):
        end = starts[i+1] if i+1 < len(starts) else len(text)
        sections.append(text[s:end])
    return sections


def _check_backup_log(path: Path, label: str, max_age_hours: float):
    """Read the most recent backup run and classify it."""
    text = _parse_log_tail(path)
    out = {'label': label, 'log': str(path)}

    if not text:
        out.update({'status': 'unknown',
                    'message': 'No log file found',
                    'last_run': None,
                    'age_hours': None})
        return out

    sections = _split_runs(text)
    last = sections[-1] if sections else text

    # Pick most recent timestamp inside the last run
    timestamps = _TS_RE.findall(last)
    if not timestamps:
        out.update({'status': 'unknown',
                    'message': 'Could not find run timestamps',
                    'last_run': None,
                    'age_hours': None})
        return out

    last_ts = datetime.strptime(timestamps[-1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=_local_tz())
    age_hours = (datetime.now().astimezone() - last_ts).total_seconds() / 3600

    # Determine success or failure
    has_fail = any(m in last for m in FAILURE_MARKERS)
    has_success = any(m in last for m in SUCCESS_MARKERS)

    if has_fail:
        status = 'failed'
        # Pick first failure line for the message
        for marker in FAILURE_MARKERS:
            idx = last.find(marker)
            if idx >= 0:
                line_start = last.rfind('\n', 0, idx) + 1
                line_end = last.find('\n', idx)
                snippet = last[line_start:line_end].strip()
                message = f"Last run failed: {snippet[:200]}"
                break
        else:
            message = 'Last run failed'
    elif age_hours > max_age_hours:
        status = 'stale'
        message = f"Last run was {int(age_hours)}h ago — expected more recent"
    elif has_success:
        status = 'ok'
        message = f"Last run succeeded {int(age_hours)}h ago"
    else:
        # Neither marker found — be honest about it
        status = 'unknown'
        message = f"Last run finished {int(age_hours)}h ago but no clear success/fail marker"

    out.update({
        'status':    status,
        'message':   message,
        'last_run':  last_ts.isoformat(),
        'age_hours': round(age_hours, 1),
    })
    return out


def check_local_csv():
    """Newest CSV file on local disk (proves CSV export step works)."""
    out = {'label': 'local_csv', 'path': str(LOCAL_CSV_DIR)}
    if not LOCAL_CSV_DIR.exists():
        out.update({'status': 'unknown', 'message': 'CSV directory does not exist',
                    'last_file': None, 'age_hours': None})
        return out
    files = sorted(LOCAL_CSV_DIR.glob('google_contacts_*.csv'),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        out.update({'status': 'failed', 'message': 'No local CSV files found',
                    'last_file': None, 'age_hours': None})
        return out
    newest = files[0]
    mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=_local_tz())
    age_hours = (datetime.now().astimezone() - mtime).total_seconds() / 3600
    size_mb = newest.stat().st_size / (1024*1024)
    out.update({
        'last_file':  newest.name,
        'size_mb':    round(size_mb, 2),
        'last_run':   mtime.isoformat(),
        'age_hours':  round(age_hours, 1),
        'status':     'ok' if age_hours < 36 else 'stale',
        'message':    f"{newest.name} ({size_mb:.1f} MB) — {int(age_hours)}h old",
    })
    return out


def check_synology_reachable(timeout=10):
    """Light SSH connectivity probe."""
    out = {'label': 'synology_reachable'}
    try:
        result = subprocess.run(
            ['ssh', '-i', SYNOLOGY_KEY,
             '-o', f'ConnectTimeout={timeout}',
             '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new',
             f'{SYNOLOGY_USER}@{SYNOLOGY_IP}',
             'echo ok'],
            capture_output=True, text=True, timeout=timeout + 5
        )
        if result.returncode == 0 and 'ok' in result.stdout:
            out.update({'status': 'ok', 'message': 'SSH reachable'})
        else:
            out.update({'status': 'failed',
                        'message': f"SSH unreachable: {(result.stderr or 'no response')[:200]}"})
    except subprocess.TimeoutExpired:
        out.update({'status': 'failed', 'message': f'SSH timed out after {timeout}s'})
    except Exception as e:
        out.update({'status': 'failed', 'message': str(e)[:200]})
    return out


def get_backup_status():
    """All backup checks combined."""
    return {
        'google_csv':   _check_backup_log(GOOGLE_CSV_LOG,  'google_csv',  max_age_hours=27),
        # 'full_server' check removed — backup disabled pending restructure
        'local_csv':    check_local_csv(),
        'synology':     check_synology_reachable(),
    }


if __name__ == '__main__':
    import json as _j
    print(_j.dumps(get_backup_status(), indent=2, default=str))
