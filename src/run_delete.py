#!/usr/bin/env python3
"""Delete sync runner — every 2 hours via cron.
Detects deletions on either side and applies soft-delete flags."""
import os, sys, fcntl, json
sys.path.insert(0, '/root/contact-sync/src')
from logger import get_logger
from airtable_api import AirtableClient
from auth import get_credentials
from google_api import build_service
from delete_engine import run_delete_sync
from state_manager import initialize_db

LOCK_FILE = "/root/contact-sync/data/delete.lock"
CONFIG_PATH = "/root/contact-sync/config/settings.json"

logger = get_logger("delete")

def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.warning("Previous delete sync still running — skipping")
        lock.close()
        return

    logger.info("=" * 60)
    logger.info("Delete Sync Started")
    logger.info("=" * 60)

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        initialize_db()
        safety = config.get("safety", {})
        dry_run = safety.get("dry_run", False)
        airtable = AirtableClient(
            config['airtable']['token'],
            config['airtable']['base_id'],
            config['airtable']['table_id']
        )
        account = config['google_accounts'][0]
        creds = get_credentials(account)
        service = build_service(creds)
        stats = run_delete_sync(airtable, service, dry_run=dry_run)
        logger.info(f"Delete sync done: {stats}")
    except Exception as e:
        logger.error(f"Delete sync failed: {e}", exc_info=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

if __name__ == "__main__":
    main()
