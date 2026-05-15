#!/usr/bin/env python3
"""Pull runner — every 30 minutes via cron.
Pulls Google changes into Airtable. Uses incremental sync token."""
import os, sys, fcntl, json
sys.path.insert(0, '/root/contact-sync/src')
from logger import get_logger
from airtable_api import AirtableClient
from auth import get_credentials
from google_api import build_service, fetch_all_contacts
from sync_engine import process_contacts
from state_manager import initialize_db, get_sync_token, save_sync_token, start_run, finish_run

LOCK_FILE = "/root/contact-sync/data/pull.lock"
CONFIG_PATH = "/root/contact-sync/config/settings.json"

logger = get_logger("pull")

def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.warning("Previous pull still running — skipping")
        lock.close()
        return

    logger.info("=" * 60)
    logger.info("Pull (Direction 1) Started")
    logger.info("=" * 60)

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        initialize_db()
        safety = config.get("safety", {})
        dry_run = safety.get("dry_run", False)
        sync_cfg = config.get("sync", {"page_size": 200, "google_request_delay_seconds": 1})

        airtable = AirtableClient(
            config['airtable']['token'],
            config['airtable']['base_id'],
            config['airtable']['table_id']
        )

        for account in config['google_accounts']:
            if not account.get("enabled", True):
                continue
            logger.info(f"Processing account: {account['email']}")
            run_id = start_run(account['id'])
            try:
                creds = get_credentials(account)
                service = build_service(creds)
                sync_token = get_sync_token(account['id'])
                if sync_token:
                    logger.info("Using sync token for incremental pull")
                else:
                    logger.info("No sync token — doing full pull")
                raw_contacts, new_sync_token = fetch_all_contacts(
                    service=service,
                    account_id=account['id'],
                    sync_token=sync_token,
                    page_size=sync_cfg.get('page_size', 200),
                    delay=sync_cfg.get('google_request_delay_seconds', 1)
                )
                stats = process_contacts(
                    raw_contacts=raw_contacts,
                    airtable_client=airtable,
                    account=account,
                    dry_run=dry_run
                )
                if new_sync_token and not dry_run:
                    save_sync_token(account['id'], new_sync_token)
                    logger.info(f"Sync token saved for {account['email']}")
                stats['status'] = 'done'
                finish_run(run_id, stats)
                logger.info(f"Pull done: {stats}")
            except Exception as e:
                logger.error(f"Pull error for {account['email']}: {e}", exc_info=True)
                finish_run(run_id, {'status': 'error', 'errors': 1})

    except Exception as e:
        logger.error(f"Pull fatal: {e}", exc_info=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

if __name__ == "__main__":
    main()
