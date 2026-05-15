#!/usr/bin/env python3
"""Background updater — pulls Airtable + Google counts and computes mismatches.
Writes results to /root/contact-sync-dashboard/data/cache.db.
Runs via cron every 5 minutes. Safe to fail (next run retries).

Keys written to metrics table:
  airtable_count           Total Airtable records
  google_count             Total Google contacts (unique by ID)
  matched_pairs            Records linked by Google Contact ID, present in both
  airtable_only_phones     Unique canonical phones in Airtable not in Google
  google_only_phones       Unique canonical phones in Google not in Airtable
  mismatch_count           Records where AT name/email differs from Google
  pending_deletion_count   Airtable records with Skip Delete=TRUE & Pending Deletion Since set
  duplicates_in_airtable   Duplicate Dedup Phone count (#phones with 2+ records)
"""
import os, sys, json, sqlite3, re, traceback
from datetime import datetime, timezone

# Import sync system modules
sys.path.insert(0, '/root/contact-sync/src')

CACHE_DB = '/root/contact-sync-dashboard/data/cache.db'
LOCK_FILE = '/root/contact-sync-dashboard/data/update_counts.lock'

def smart_norm(p):
    if not p: return ''
    d = re.sub(r'\D', '', p.split(' ::: ')[0])
    if not d: return ''
    if d.startswith('27'): d = d[2:]
    if d.startswith('0'):  d = d[1:]
    return d

def set_metric(c, key, value):
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        "INSERT INTO metrics (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now)
    )

def main():
    # File lock to prevent overlapping runs
    import fcntl
    lock = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Previous update still running — skipping", file=sys.stderr)
        return

    started = datetime.now(timezone.utc).isoformat()
    cache = sqlite3.connect(CACHE_DB)
    cc = cache.cursor()
    cc.execute("INSERT INTO update_log (started_at, status) VALUES (?, 'running')", (started,))
    run_id = cc.lastrowid
    cache.commit()

    try:
        from airtable_api import AirtableClient
        from auth import get_credentials
        from googleapiclient.discovery import build

        sync_cfg = json.load(open('/root/contact-sync/config/settings.json'))
        airtable = AirtableClient(
            sync_cfg['airtable']['token'],
            sync_cfg['airtable']['base_id'],
            sync_cfg['airtable']['table_id']
        )
        account = sync_cfg['google_accounts'][0]
        creds = get_credentials(account)
        service = build('people', 'v1', credentials=creds, cache_discovery=False)

        # --- Airtable ---
        records = airtable.get_all_records(force_refresh=True)
        at_count = len(records)
        at_phones = set()
        at_by_gid = {}            # gid -> field dict
        pending_deletion = 0
        phone_buckets = {}        # canonical phone -> list of records
        for r in records:
            f = r.get('fields', {})
            n = smart_norm(f.get('Dedup Phone', ''))
            if n:
                at_phones.add(n)
                phone_buckets.setdefault(n, []).append(r)
            gid = (f.get('Google Contact ID') or '').strip()
            if gid:
                at_by_gid[gid] = f
            # Pending deletion = Skip Delete checked AND Pending Deletion Since set
            if f.get('Skip Delete') and f.get('Pending Deletion Since'):
                pending_deletion += 1
        duplicates_in_airtable = sum(1 for p, recs in phone_buckets.items() if len(recs) > 1)

        # --- Google ---
        google_ids = set()
        google_by_id = {}
        google_phones = set()
        pt = None
        while True:
            kw = {'resourceName':'people/me','pageSize':1000,
                  'personFields':'names,phoneNumbers,emailAddresses'}
            if pt: kw['pageToken']=pt
            r = service.people().connections().list(**kw).execute()
            for c in r.get('connections', []):
                rn = c.get('resourceName','')
                if not rn: continue
                google_ids.add(rn)
                names  = c.get('names', [{}])
                nm     = names[0] if names else {}
                emails = c.get('emailAddresses', [])
                google_by_id[rn] = {
                    'given':  (nm.get('givenName')  or '').strip(),
                    'family': (nm.get('familyName') or '').strip(),
                    'email':  (emails[0].get('value') if emails else '').strip(),
                }
                for pn in c.get('phoneNumbers', []):
                    s = smart_norm(pn.get('value',''))
                    if s: google_phones.add(s)
            pt = r.get('nextPageToken')
            if not pt: break

        google_count = len(google_ids)

        # --- Cross-checks ---
        matched_pairs = sum(1 for gid in at_by_gid if gid in google_ids)
        only_in_airtable = len(at_phones - google_phones)
        only_in_google   = len(google_phones - at_phones)

        # Field-level mismatch count
        mismatch_count = 0
        for gid, af in at_by_gid.items():
            if gid not in google_by_id: continue
            g = google_by_id[gid]
            at_first = (af.get('First Name') or '').strip()
            at_last  = (af.get('Last Name')  or '').strip()
            at_email = (af.get('Email') or af.get('Clean Email','') or '').strip()
            if (at_first != g['given'] or
                at_last  != g['family'] or
                (at_email and at_email != g['email']) or
                (g['email'] and at_email != g['email'])):
                mismatch_count += 1

        # Write all metrics
        set_metric(cc, 'airtable_count', at_count)
        set_metric(cc, 'google_count', google_count)
        set_metric(cc, 'matched_pairs', matched_pairs)
        set_metric(cc, 'airtable_only_phones', only_in_airtable)
        set_metric(cc, 'google_only_phones', only_in_google)
        set_metric(cc, 'mismatch_count', mismatch_count)
        set_metric(cc, 'pending_deletion_count', pending_deletion)
        set_metric(cc, 'duplicates_in_airtable', duplicates_in_airtable)

        finished = datetime.now(timezone.utc).isoformat()
        cc.execute("UPDATE update_log SET finished_at=?, status='ok' WHERE id=?",
                   (finished, run_id))
        cache.commit()
        print(f"✓ Updated: AT={at_count} G={google_count} matched={matched_pairs} "
              f"mismatches={mismatch_count} pending={pending_deletion} dups={duplicates_in_airtable}")

    except Exception as e:
        err = traceback.format_exc()
        cc.execute("UPDATE update_log SET finished_at=?, status='error', error=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(), err[:1000], run_id))
        cache.commit()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cache.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

if __name__ == '__main__':
    main()
