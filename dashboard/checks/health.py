"""health.py — combines all checks into a single overall status.

Output:
  overall_status   green | yellow | red | gray
  overall_label    short headline ("All systems operational")
  overall_summary  one-sentence plain English summary
  components       list of {name, status, message} for each check
  alerts           list of {severity, area, message, suggested_action}
                   sorted by severity (red first)
"""
from . import sync_status, queue as queue_check, cron_health, backups, compare


SEVERITY_RANK = {'red': 0, 'yellow': 1, 'green': 2, 'gray': 3}


def _add(alerts, severity, area, message, action=None):
    alerts.append({
        'severity': severity, 'area': area,
        'message': message, 'action': action,
    })


def _evaluate_cron(cron_data, alerts):
    """Verify each cron job ran on schedule."""
    components = []
    for label, info in cron_data.items():
        status_map = {'ok': 'green', 'delayed': 'yellow', 'stalled': 'red', 'unknown': 'gray'}
        st = status_map.get(info.get('status'), 'gray')
        components.append({'name': f'Cron · {label}', 'status': st, 'message': info.get('message', '')})
        if st == 'red':
            _add(alerts, 'red', 'cron',
                 f"Job '{label}' has not run on schedule. {info.get('message','')}",
                 "Check /var/log/syslog for cron errors, or run the job manually to diagnose.")
        elif st == 'yellow':
            _add(alerts, 'yellow', 'cron',
                 f"Job '{label}' is running late. {info.get('message','')}", None)
    return components


def _evaluate_sync(sync_data, alerts):
    """Sync direction health (pull / push / delete)."""
    components = []
    for direction, info in sync_data.items():
        name = f'Sync · {direction}'
        if not info.get('available'):
            components.append({'name': name, 'status': 'gray',
                                'message': 'No recent runs found in logs'})
            continue
        errors = info.get('errors', 0)
        if direction == 'delete' and info.get('aborted_by_safety', 0) > 0:
            components.append({'name': name, 'status': 'red',
                                'message': f"Safety abort: cascade threshold exceeded"})
            _add(alerts, 'red', 'sync',
                 'Delete sync was aborted by the 5% safety threshold.',
                 'Inspect /root/contact-sync/logs/sync.log for the abort reason before clearing the safety flag.')
        elif errors > 0:
            components.append({'name': name, 'status': 'yellow',
                                'message': f"Last run had {errors} error(s)"})
            _add(alerts, 'yellow', 'sync',
                 f"Last {direction} run had {errors} error(s).",
                 'Check the cron log for the affected records.')
        else:
            stats_msg = ''
            if direction == 'push':
                stats_msg = f"{info.get('updated', 0)} pushed"
            elif direction == 'pull':
                stats_msg = f"{info.get('updated', 0)} updated"
            elif direction == 'delete':
                stats_msg = f"{info.get('soft_flagged_airtable',0)+info.get('soft_flagged_google',0)} soft-flagged"
            components.append({'name': name, 'status': 'green',
                                'message': f"Last run clean — {stats_msg}"})
    return components


def _evaluate_backups(backup_data, alerts):
    components = []
    label_map = {
        'google_csv':   'Backup · Google CSV',
        'full_server':  'Backup · Server',
        'local_csv':    'Backup · Local CSV',
        'synology':     'Backup · Synology',
    }
    status_map = {'ok': 'green', 'stale': 'yellow', 'failed': 'red', 'unknown': 'gray'}
    for key, info in backup_data.items():
        st = status_map.get(info.get('status'), 'gray')
        components.append({'name': label_map.get(key, key), 'status': st,
                            'message': info.get('message', '')})
        if st == 'red':
            if key == 'synology':
                _add(alerts, 'red', 'backup',
                     'Synology storage is unreachable.',
                     'Check Tailscale connection on the Synology and SSH key access.')
            elif key in ('google_csv', 'full_server'):
                _add(alerts, 'yellow', 'backup',
                     f"{label_map[key]} failed on its last run.",
                     'Local backup may still be intact — check the backup log for the exact error.')


    return components


def _evaluate_queue(queue_data, compare_data, alerts):
    components = []
    # Lock files — if a lock exists but no process holds it for >30 min, suggest stale
    for lock_name, info in queue_data.get('locks', {}).items():
        if not info.get('exists'):
            continue
        if info.get('held_by_running_process'):
            components.append({
                'name':    f'Lock · {lock_name}',
                'status':  'green',
                'message': f"Currently held by a running {lock_name} process",
            })
        else:
            age = info.get('age_seconds', 0)
            if age > 3600 and lock_name != 'full':
                components.append({
                    'name': f'Lock · {lock_name}', 'status': 'yellow',
                    'message': f"Stale lock file ({int(age/60)} min old, no process)",
                })

    # Mismatch count (from cache)
    if compare_data.get('available'):
        mm = compare_data.get('mismatch_count', 0)
        if mm == 0:
            components.append({'name': 'Sync · Mismatches', 'status': 'green',
                                'message': 'No drift between Airtable and Google'})
        elif mm < 20:
            components.append({'name': 'Sync · Mismatches', 'status': 'yellow',
                                'message': f"{mm} records differ between Airtable and Google"})
            _add(alerts, 'yellow', 'mismatch',
                 f"{mm} records have name/email drift between Airtable and Google.",
                 'Run /tmp/find_and_fix_mismatches.py to reconcile.')
        else:
            components.append({'name': 'Sync · Mismatches', 'status': 'red',
                                'message': f"{mm} records mismatched — investigate"})
            _add(alerts, 'red', 'mismatch',
                 f"{mm} records have drift between Airtable and Google.",
                 'Run /tmp/find_and_fix_mismatches.py --apply to reconcile, or inspect the report CSV.')

        # Pending deletions
        pd = compare_data.get('pending_deletion_count', 0)
        if pd > 0:
            components.append({'name': 'Soft-delete queue', 'status': 'yellow' if pd > 50 else 'green',
                                'message': f"{pd} contacts pending deletion (30-day grace)"})

        # Duplicates
        du = compare_data.get('duplicates_in_airtable', 0)
        if du > 0:
            components.append({'name': 'Duplicates · Airtable', 'status': 'yellow' if du > 100 else 'green',
                                'message': f"{du} phone numbers appear in 2+ records"})
    return components


def get_overall_health():
    sync_data    = sync_status.get_status_all()
    queue_data   = queue_check.get_state()
    cron_data    = cron_health.get_cron_health()
    backup_data  = backups.get_backup_status()
    compare_data = compare.get_comparison()

    alerts = []
    components = []
    components += _evaluate_sync(sync_data, alerts)
    components += _evaluate_cron(cron_data, alerts)
    components += _evaluate_backups(backup_data, alerts)
    components += _evaluate_queue(queue_data, compare_data, alerts)

    # Determine overall status: worst-severity wins
    severities = {a['severity'] for a in alerts}
    if 'red' in severities:
        overall = 'red'
        label   = 'Issues detected — action required'
    elif 'yellow' in severities:
        overall = 'yellow'
        label   = 'Minor issues — monitoring recommended'
    elif components:
        overall = 'green'
        label   = 'All systems operational'
    else:
        overall = 'gray'
        label   = 'No data yet — initial run pending'

    summary_parts = []
    if compare_data.get('available'):
        summary_parts.append(
            f"Airtable: {compare_data['airtable_count']:,} · "
            f"Google: {compare_data['google_count']:,} · "
            f"Matched: {compare_data['matched_pairs']:,}"
        )
    if alerts:
        summary_parts.append(f"{len(alerts)} alert{'s' if len(alerts)>1 else ''} need attention")
    summary = ' · '.join(summary_parts) if summary_parts else 'System status unknown'

    # Sort alerts by severity
    alerts.sort(key=lambda a: SEVERITY_RANK.get(a['severity'], 99))

    return {
        'overall_status':  overall,
        'overall_label':   label,
        'overall_summary': summary,
        'components':      components,
        'alerts':          alerts,
        'raw': {
            'sync':    sync_data,
            'cron':    cron_data,
            'backups': backup_data,
            'compare': compare_data,
            'queue':   queue_data,
        },
    }


if __name__ == '__main__':
    import json as _j
    print(_j.dumps(get_overall_health(), indent=2, default=str))
