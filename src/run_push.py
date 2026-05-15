#!/usr/bin/env python3
"""Push runner — every 2 minutes via cron.
Only pushes user-edited Airtable records to Google.
Safe to fail/timeout; next run will pick up where it left off."""
import os, sys, fcntl, json
sys.path.insert(0, '/root/contact-sync/src')
from logger import get_logger
from airtable_api import AirtableClient
from push_engine import run_push
from state_manager import initialize_db

LOCK_FILE = "/root/contact-sync/data/push.lock"
CONFIG_PATH = "/root/contact-sync/config/settings.json"

logger = get_logger("push")

def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        # Previous push still running — skip silently (don't spam logs)
        lock.close()
        return

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        initialize_db()
        airtable = AirtableClient(
            config['airtable']['token'],
            config['airtable']['base_id'],
            config['airtable']['table_id']
        )
        account = config['google_accounts'][0]
        result = run_push(airtable, account, dry_run=False)
        # Only log if something actually happened
        if result.get("checked", 0) > 0:
            logger.info(f"Push run: {result}")
    except Exception as e:
        logger.error(f"Push failed: {e}", exc_info=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

if __name__ == "__main__":
    main()
