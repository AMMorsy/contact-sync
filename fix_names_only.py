#!/usr/bin/env python3
"""
Names-only re-sync — fast version.
Patches names directly using Google Contact ID. No full Airtable load.
"""
import os, sys, json, time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from auth import get_credentials
from googleapiclient.discovery import build
from airtable_api import AirtableClient

def get_raw_name(name_obj):
    first   = (name_obj.get("givenName") or "").strip()
    last    = (name_obj.get("familyName") or "").strip()
    display = (name_obj.get("displayName") or "").strip()
    if not first and not last and display:
        parts = display.split()
        first = parts[0]
        last  = " ".join(parts[1:]) if len(parts) > 1 else ""
    full = f"{first} {last}".strip() or display
    return first, last, full

def main():
    config_path = os.path.join(PROJECT_ROOT, 'config', 'settings.json')
    with open(config_path) as f:
        config = json.load(f)
    account = config['google_accounts'][0]

    print("Loading credentials...")
    creds   = get_credentials(account)
    service = build('people', 'v1', credentials=creds, cache_discovery=False)

    airtable = AirtableClient(
        config['airtable']['token'],
        config['airtable']['base_id'],
        config['airtable']['table_id']
    )

    # Pull all Google contacts
    print("Pulling all contacts from Google...")
    all_google = []
    page_token = None
    page = 1
    while True:
        kwargs = {
            'resourceName': 'people/me',
            'pageSize': 1000,
            'personFields': 'names,metadata'
        }
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.people().connections().list(**kwargs).execute()
        contacts = resp.get('connections', [])
        all_google.extend(contacts)
        print(f"Page {page}: {len(contacts)} | Total: {len(all_google)}")
        page_token = resp.get('nextPageToken')
        page += 1
        if not page_token:
            break
        time.sleep(0.3)

    print(f"\nTotal: {len(all_google)} contacts from Google")
    print("Searching Airtable by Google Contact ID and patching names...\n")

    updated = skipped = not_found = errors = 0

    for i, contact in enumerate(all_google, 1):
        resource_name = contact.get('resourceName', '')
        names = contact.get('names', [])

        if not names:
            skipped += 1
            continue

        first, last, full = get_raw_name(names[0])
        if not full:
            skipped += 1
            continue

        # Search Airtable by Google Contact ID
        try:
            results = airtable.search_by_field('Google Contact ID', resource_name)
            if not results:
                not_found += 1
                continue

            record_id = results[0]['id']
            airtable.patch_record(record_id, {
                'First Name': first,
                'Last Name':  last,
                'Full Name':  full
            })
            print(f"[{i}/{len(all_google)}] UPDATED: {full}")
            updated += 1
            time.sleep(0.25)

        except Exception as e:
            print(f"ERROR {resource_name}: {e}")
            errors += 1
            time.sleep(1)

    print(f"\n{'='*50}")
    print(f"Done! Updated: {updated} | Skipped: {skipped} | Not found: {not_found} | Errors: {errors}")
    print(f"{'='*50}")

if __name__ == '__main__':
    main()
