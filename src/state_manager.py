import sqlite3
import os
from datetime import datetime

DB_PATH = "/root/contact-sync/data/sync_state.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_tokens (
            account_id TEXT PRIMARY KEY,
            sync_token TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            contacts_pulled INTEGER DEFAULT 0,
            contacts_created INTEGER DEFAULT 0,
            contacts_updated INTEGER DEFAULT 0,
            contacts_skipped INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            status TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dedup_cache (
            canonical_phone TEXT PRIMARY KEY,
            canonical_email TEXT,
            airtable_record_id TEXT,
            google_contact_id TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS flagged_duplicates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incoming_phone TEXT,
            incoming_email TEXT,
            incoming_name TEXT,
            matched_airtable_id TEXT,
            match_type TEXT,
            flagged_at TEXT,
            resolved INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_sync_token(account_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT sync_token FROM sync_tokens WHERE account_id = ?",
        (account_id,)
    ).fetchone()
    conn.close()
    return row["sync_token"] if row else None

def save_sync_token(account_id, token):
    conn = get_connection()
    conn.execute("""
        INSERT INTO sync_tokens (account_id, sync_token, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            sync_token = excluded.sync_token,
            updated_at = excluded.updated_at
    """, (account_id, token, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def start_run(account_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO run_log (account_id, started_at, status)
        VALUES (?, ?, 'running')
    """, (account_id, datetime.utcnow().isoformat()))
    run_id = c.lastrowid
    conn.commit()
    conn.close()
    return run_id

def finish_run(run_id, stats):
    conn = get_connection()
    conn.execute("""
        UPDATE run_log SET
            finished_at = ?,
            contacts_pulled = ?,
            contacts_created = ?,
            contacts_updated = ?,
            contacts_skipped = ?,
            errors = ?,
            status = ?
        WHERE id = ?
    """, (
        datetime.utcnow().isoformat(),
        stats.get("pulled", 0),
        stats.get("created", 0),
        stats.get("updated", 0),
        stats.get("skipped", 0),
        stats.get("errors", 0),
        stats.get("status", "done"),
        run_id
    ))
    conn.commit()
    conn.close()

def upsert_dedup_cache(canonical_phone, canonical_email, airtable_record_id, google_contact_id):
    conn = get_connection()
    conn.execute("""
        INSERT INTO dedup_cache (canonical_phone, canonical_email, airtable_record_id, google_contact_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(canonical_phone) DO UPDATE SET
            canonical_email = excluded.canonical_email,
            airtable_record_id = excluded.airtable_record_id,
            google_contact_id = excluded.google_contact_id,
            updated_at = excluded.updated_at
    """, (canonical_phone, canonical_email, airtable_record_id, google_contact_id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def find_in_cache_by_phone(canonical_phone):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM dedup_cache WHERE canonical_phone = ?",
        (canonical_phone,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def find_in_cache_by_email(canonical_email):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM dedup_cache WHERE canonical_email = ?",
        (canonical_email,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def flag_duplicate(incoming_phone, incoming_email, incoming_name, matched_airtable_id, match_type):
    conn = get_connection()
    conn.execute("""
        INSERT INTO flagged_duplicates
        (incoming_phone, incoming_email, incoming_name, matched_airtable_id, match_type, flagged_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (incoming_phone, incoming_email, incoming_name, matched_airtable_id, match_type, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# ============================================================
# DELETE TRACKING — added for bi-directional delete sync
# ============================================================

def get_known_record_ids(record_type):
    """Get all known record IDs of a given type ('airtable' or 'google').
    Used to detect deletions by comparing against current API response."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM known_records WHERE record_type=?", (record_type,))
    ids = {row[0] for row in c.fetchall()}
    conn.close()
    return ids


def upsert_known_records(record_type, ids_with_pairs):
    """Bulk upsert known records.
    ids_with_pairs: list of tuples (id, airtable_id, google_id)"""
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for rid, aid, gid in ids_with_pairs:
        c.execute(
            "INSERT OR REPLACE INTO known_records "
            "(id, record_type, airtable_id, google_id, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, record_type, aid, gid, now)
        )
    conn.commit()
    conn.close()


def get_known_record_pairs(record_type):
    """Get all known records as dict {id: (airtable_id, google_id)}."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, airtable_id, google_id FROM known_records WHERE record_type=?",
        (record_type,)
    )
    result = {row[0]: (row[1], row[2]) for row in c.fetchall()}
    conn.close()
    return result


def delete_known_record(record_id, record_type):
    """Remove a record from the known table after sync deletion."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM known_records WHERE id=? AND record_type=?",
        (record_id, record_type)
    )
    conn.commit()
    conn.close()


# ============================================================
# FIELD-LEVEL WRITE TRACKING — prevents sync loops
# ============================================================

def record_sync_write(airtable_record_id, field_values):
    """Record that the sync script just wrote these field values to Airtable.
    The watcher uses this to distinguish script-writes from user-edits.
    field_values: dict like {"First Name": "John", "Last Name": "Doe"}"""
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for field_name, value in field_values.items():
        v = "" if value is None else str(value)
        c.execute(
            "INSERT OR REPLACE INTO sync_writes "
            "(airtable_record_id, field_name, last_written_value, written_at) "
            "VALUES (?, ?, ?, ?)",
            (airtable_record_id, field_name, v, now)
        )
    conn.commit()
    conn.close()


def get_last_sync_writes(airtable_record_id):
    """Get the last values the sync script wrote for a record.
    Returns dict {field_name: last_written_value} or {} if not found."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT field_name, last_written_value FROM sync_writes "
        "WHERE airtable_record_id=?",
        (airtable_record_id,)
    )
    result = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return result


def is_user_edit(airtable_record_id, current_fields, watched_fields):
    """Compare current Airtable field values to what the script last wrote.
    Returns True if any watched field has a value different from script's last write
    (i.e., a user edited it). Returns False if all values match (script's own write)."""
    last_writes = get_last_sync_writes(airtable_record_id)
    if not last_writes:
        # Never tracked → treat as user edit (will be tracked after first sync)
        return True
    for field in watched_fields:
        current = "" if current_fields.get(field) is None else str(current_fields.get(field, ""))
        last    = last_writes.get(field, "")
        if current.strip() != last.strip():
            return True
    return False
