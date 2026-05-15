import sys
import os
import json
import fcntl
import subprocess
import signal
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def kill_stuck_processes(max_age_minutes=20):
    """Kill ALL OTHER python3 main.py processes — only ONE run should ever
    be active at a time. Also kill the parent shell if it's a cron wrapper.
    Then remove the stale lock file so the new run can acquire it cleanly."""
    try:
        my_pid = os.getpid()
        my_ppid = os.getppid()
        result = subprocess.run(
            ['ps', '-eo', 'pid,ppid,etimes,cmd'],
            capture_output=True, text=True, timeout=10
        )
        max_seconds = max_age_minutes * 60
        killed_any = False
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
                etimes = int(parts[2])
                cmd = parts[3]
            except ValueError:
                continue
            if pid == my_pid or pid == my_ppid:
                continue
            is_main_py = ('python3' in cmd and '/root/contact-sync/src/main.py' in cmd)
            is_cron_wrapper = ('contact-sync/src/main.py' in cmd and '/bin/sh' in cmd)
            if (is_main_py or is_cron_wrapper) and etimes > max_seconds:
                print(f"[STUCK PROTECTION] Killing PID {pid} (running {etimes}s): {cmd[:80]}")
                try:
                    os.kill(pid, 9)
                    killed_any = True
                except Exception as e:
                    print(f"  failed: {e}")
        if killed_any:
            time.sleep(2)
            try:
                os.remove("/root/contact-sync/data/sync.lock")
                print("[STUCK PROTECTION] Removed stale lock file")
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[STUCK PROTECTION] Could not remove lock: {e}")
    except Exception as e:
        print(f"[STUCK PROTECTION] Check failed: {e}")


# Kill stuck processes BEFORE trying to acquire the lock
kill_stuck_processes(max_age_minutes=20)
time.sleep(1)

from logger import get_logger
from state_manager import initialize_db, get_sync_token, save_sync_token, start_run, finish_run
from auth import get_credentials
from google_api import build_service, fetch_all_contacts
from airtable_api import AirtableClient
from sync_engine import process_contacts
from push_engine import run_push
from delete_engine import run_delete_sync

logger = get_logger()

CONFIG_PATH = "/root/contact-sync/config/settings.json"
LOCK_FILE = "/root/contact-sync/data/sync.lock"

# ============================================================
# GLOBAL SCRIPT TIMEOUT — kill self if running longer than this
# ============================================================
SCRIPT_TIMEOUT_MINUTES = 15

def _timeout_handler(signum, frame):
    print(f"[GLOBAL TIMEOUT] Script exceeded {SCRIPT_TIMEOUT_MINUTES} minutes — exiting forcefully")
    try:
        from logger import get_logger
        get_logger().error(f"Global timeout {SCRIPT_TIMEOUT_MINUTES}min reached — script killed itself")
    except Exception:
        pass
    os._exit(1)

# Install timeout (SIGALRM only available on Unix — safe to no-op elsewhere)
try:
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT_MINUTES * 60)
except Exception:
    pass


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def main():
    # Prevent overlapping runs
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.warning("Another sync is already running. Skipping this run.")
        lock_file.close()
        return

    logger.info("=" * 60)
    logger.info("Contact Sync Started")
    logger.info("=" * 60)

    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        sys.exit(1)

    initialize_db()

    safety = config.get("safety", {})
    dry_run = safety.get("dry_run", False)
    google_write_enabled = safety.get("google_write_enabled", False)

    if dry_run:
        logger.info("DRY RUN MODE — no writes will happen")

    airtable_cfg = config["airtable"]
    sync_cfg = config["sync"]

    airtable = AirtableClient(
        token=airtable_cfg["token"],
        base_id=airtable_cfg["base_id"],
        table_id=airtable_cfg["table_id"],
        delay=sync_cfg["airtable_request_delay_seconds"],
        max_retries=sync_cfg["max_retries"],
        backoff=sync_cfg["retry_backoff_seconds"]
    )

    # =========================================================
    # DIRECTION 1: Google → Airtable
    # =========================================================
    logger.info("--- Direction 1: Google → Airtable ---")

    for account in config["google_accounts"]:
        if not account.get("active", False):
            logger.info(f"Skipping inactive account: {account['email']}")
            continue

        logger.info(f"Processing account: {account['email']}")
        run_id = start_run(account["id"])

        try:
            creds = get_credentials(account)
            service = build_service(creds)
            sync_token = get_sync_token(account["id"])

            if sync_token:
                logger.info("Using sync token for incremental pull")
            else:
                logger.info("No sync token found — doing full pull")

            raw_contacts, new_sync_token = fetch_all_contacts(
                service=service,
                account_id=account["id"],
                sync_token=sync_token,
                page_size=sync_cfg["page_size"],
                delay=sync_cfg["google_request_delay_seconds"]
            )

            stats = process_contacts(
                raw_contacts=raw_contacts,
                airtable_client=airtable,
                account=account,
                dry_run=dry_run
            )

            if new_sync_token and not dry_run:
                save_sync_token(account["id"], new_sync_token)
                logger.info(f"Sync token saved for {account['email']}")

            stats["status"] = "done"
            finish_run(run_id, stats)
            logger.info(f"Direction 1 done for {account['email']}: {stats}")

        except Exception as e:
            logger.error(f"Direction 1 fatal error for {account['email']}: {e}")
            finish_run(run_id, {"status": "error", "errors": 1})

    # =========================================================
    # DELETE SYNC: bi-directional deletion detection
    # =========================================================
    logger.info("--- Delete Sync: Airtable ↔ Google ---")

    if not google_write_enabled:
        logger.info("Google write disabled — skipping delete sync.")
    else:
        for account in config["google_accounts"]:
            if not account.get("active", False):
                continue
            try:
                creds = get_credentials(account)
                service = build_service(creds)
                delete_stats = run_delete_sync(
                    airtable_client=airtable,
                    service=service,
                    dry_run=dry_run
                )
                logger.info(f"Delete sync done for {account['email']}: {delete_stats}")
            except Exception as e:
                logger.error(f"Delete sync fatal error for {account['email']}: {e}")

    # =========================================================
    # DIRECTION 2: Airtable → Google
    # =========================================================
    logger.info("--- Direction 2: Airtable → Google ---")

    if not google_write_enabled:
        logger.info("Google write is disabled in settings. Skipping Direction 2.")
    else:
        for account in config["google_accounts"]:
            if not account.get("active", False):
                continue
            logger.info(f"Pushing to Google account: {account['email']}")
            try:
                push_stats = run_push(
                    airtable_client=airtable,
                    account=account,
                    dry_run=dry_run
                )
                logger.info(f"Direction 2 done for {account['email']}: {push_stats}")
            except Exception as e:
                logger.error(f"Direction 2 fatal error for {account['email']}: {e}")

    logger.info("Contact Sync Finished")
    logger.info("=" * 60)

    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

if __name__ == "__main__":
    main()
