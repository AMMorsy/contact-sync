"""cron_health.py — verifies each scheduled job has run within expected window.

Each job is checked against its expected frequency. If the last run is older than
the alert threshold, the job is flagged as 'delayed' or 'stalled'.
"""
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

LOG_DIR = Path('/root/contact-sync/logs')

# Match any ISO timestamp prefix "YYYY-MM-DD HH:MM:SS"
_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

# Per-job expectations
JOBS = {
    'push':           {'log': 'cron-push.log',   'every_seconds': 180,    'alert_seconds': 600},     # every 3 min, alert if >10 min
    'pull':           {'log': 'cron-pull.log',   'every_seconds': 1800,   'alert_seconds': 4200},    # every 30 min, alert if >70 min
    'delete':         {'log': 'cron-delete.log', 'every_seconds': 7200,   'alert_seconds': 14400},   # every 2h, alert if >4h
    'full':           {'log': 'cron.log',        'every_seconds': 86400,  'alert_seconds': 172800},  # daily, alert if >2 days
}

# Dashboard updater (also has its own log)
DASHBOARD_UPDATER = {
    'updater': {'log': '/root/contact-sync-dashboard/logs/updater.log',
                'every_seconds': 300, 'alert_seconds': 900}
}


def _last_log_timestamp(path: Path) -> Optional[datetime]:
    """Read the tail of a log file, return the last timestamp as a LOCAL-aware datetime.

    Sync logs (sync_engine.py logger) use Python logging's default %(asctime)s,
    which writes local time WITHOUT a timezone offset. We parse it as naive,
    then attach the SYSTEM local timezone so comparisons against datetime.now(local)
    are correct."""
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with path.open('rb') as f:
            f.seek(max(0, size - 50_000))
            text = f.read().decode('utf-8', errors='ignore')
        matches = list(_TS_RE.finditer(text))
        if not matches:
            return None
        ts = matches[-1].group(1)
        # Parse naive, then localize to server's tz
        naive = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        local_tz = datetime.now().astimezone().tzinfo
        return naive.replace(tzinfo=local_tz)
    except Exception:
        return None


def _status_for(label: str, spec: dict, base_dir: Path):
    log_path = spec['log']
    path = Path(log_path) if log_path.startswith('/') else base_dir / log_path
    last = _last_log_timestamp(path)
    now = datetime.now().astimezone()  # local tz, aware
    out = {
        'job':           label,
        'log':           str(path),
        'every_seconds': spec['every_seconds'],
        'alert_seconds': spec['alert_seconds'],
    }
    if last is None:
        out.update({
            'last_run':         None,
            'age_seconds':      None,
            'status':           'unknown',
            'message':          'No log entries found',
        })
        return out
    age = (now - last).total_seconds()
    out['last_run']    = last.isoformat()
    out['age_seconds'] = round(age, 1)
    if age > spec['alert_seconds']:
        out['status']  = 'stalled'
        out['message'] = f"Last run was {_human_age(age)} ago — expected every {_human_age(spec['every_seconds'])}"
    elif age > spec['every_seconds'] * 1.5:
        out['status']  = 'delayed'
        out['message'] = f"Slight delay — last run {_human_age(age)} ago"
    else:
        out['status']  = 'ok'
        out['message'] = f"Last run {_human_age(age)} ago"
    return out


def _human_age(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h"


def get_cron_health():
    """All sync jobs + the dashboard updater."""
    results = {}
    for label, spec in JOBS.items():
        results[label] = _status_for(label, spec, LOG_DIR)
    for label, spec in DASHBOARD_UPDATER.items():
        results[label] = _status_for(label, spec, LOG_DIR)
    return results


if __name__ == '__main__':
    import json as _j
    print(_j.dumps(get_cron_health(), indent=2, default=str))
