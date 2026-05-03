# Contact Sync — Bi-Directional Google Contacts ⇄ Airtable

A production-grade Python sync engine that keeps **Google Contacts** and an **Airtable base** perfectly aligned in both directions, with phone normalization, fuzzy deduplication, conflict resolution, deletion propagation, and loop-prevention guarantees.

> One source of truth. Two systems. Zero duplicates. No infinite sync loops.

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Architecture](#architecture)
- [Repository structure](#repository-structure)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Step-by-step setup](#step-by-step-setup)
  - [1. Clone and install](#1-clone-and-install)
  - [2. Create a Google Cloud OAuth client](#2-create-a-google-cloud-oauth-client)
  - [3. Get Airtable credentials](#3-get-airtable-credentials)
  - [4. Configure the project](#4-configure-the-project)
  - [5. Initialize the database](#5-initialize-the-database)
  - [6. First-run authorization](#6-first-run-authorization)
  - [7. Schedule automatic sync](#7-schedule-automatic-sync)
- [Required Airtable schema](#required-airtable-schema)
- [How the sync works](#how-the-sync-works)
- [Configuration reference](#configuration-reference)
- [Operations & troubleshooting](#operations--troubleshooting)
- [Utilities](#utilities)
- [Safety switches](#safety-switches)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Why this exists

Most CRM ↔ contacts integrations break in three places:

1. **Phones aren't normalized**, so the same number ends up duplicated under different formats (`+27 82 ...`, `082 ...`, `00 27 82 ...`).
2. **Sync loops** — system A writes to system B, which triggers system B's "updated at" timestamp, which makes system A think there's a new edit and pushes it back, and so on, forever.
3. **Deletions don't propagate**, leaving orphan records in one system after they've been removed from the other.

This project solves all three.

---

## Features

- ✅ **True bi-directional sync** — Google → Airtable AND Airtable → Google
- ✅ **Bi-directional deletion sync** — delete in either system, both stay aligned
- ✅ **E.164 phone normalization** with split-and-deduplicate for concatenated numbers
- ✅ **Multi-layer deduplication** — exact phone, exact email, fuzzy name+email matching
- ✅ **Loop-proof** — tracks the exact field values the script wrote so user-edits and script-writes never get confused
- ✅ **Conflict resolution** — Google-wins inside a configurable tolerance window
- ✅ **Incremental pulls** via Google sync tokens (full pull on first run, deltas after)
- ✅ **Idempotent writes** — only patches fields that actually differ
- ✅ **Safety switches** — global `dry_run`, `google_write_enabled`, `google_delete_enabled`
- ✅ **Stuck-process protection** — auto-kills hung runs and stale lock files
- ✅ **Global timeout** — script self-terminates after 15 min to prevent runaway crons
- ✅ **Skip-Delete flag** per record to protect specific contacts from deletion sync
- ✅ **SQLite state** — sync tokens, dedup cache, run log, known-records, write tracker
- ✅ **Daily-rotated logs** — separate `sync.log` and `errors.log`

---

## Architecture

```
┌──────────────────┐                                   ┌──────────────────┐
│  Google Contacts │                                   │     Airtable     │
└────────┬─────────┘                                   └────────┬─────────┘
         │                                                      │
         │   1. Pull (incremental via syncToken)                │
         │  ──────────────────────────────►   ┌───────────┐     │
         │                                    │  sync_    │     │
         │                                    │  engine   │ ──► Upsert/Patch
         │                                    └───────────┘     │
         │                                                      │
         │   2. Bi-directional delete detection                 │
         │  ◄─────────────────  ┌───────────┐  ─────────────►   │
         │                      │  delete_  │                   │
         │                      │  engine   │                   │
         │                      └───────────┘                   │
         │                                                      │
         │   3. Push user-edits Airtable → Google               │
         │                      ┌───────────┐                   │
         │                      │  push_    │  ◄─── watched     │
         │  ◄──────────────────  │  engine   │       fields     │
         │                      └───────────┘                   │
         │                                                      │
         └──────────────► SQLite: sync_state.db ◄───────────────┘
                          (tokens, dedup cache, run log,
                           known_records, sync_writes)
```

Pipeline order on every run:

1. **Direction 1** — Google → Airtable (with dedup)
2. **Delete sync** — detect deletions in either system, propagate
3. **Direction 2** — Airtable → Google (only user-edited records)

---

## Repository structure

```
contact-sync/
├── config/
│   ├── accounts/                    # OAuth tokens (gitignored)
│   └── settings.example.json        # Copy → settings.json
├── credentials/
│   └── google_client_secret.example.json   # Copy → google_client_secret.json
├── data/
│   ├── sync_state.db                # SQLite state (gitignored)
│   └── sync.lock                    # Process-overlap lock
├── logs/                            # sync.log, errors.log (gitignored)
├── src/
│   ├── main.py                      # Entry point — orchestrates the run
│   ├── auth.py                      # Google OAuth flow + token refresh
│   ├── google_api.py                # Read-side: people.connections.list
│   ├── google_writer.py             # Write-side: create/update/delete contact
│   ├── airtable_api.py              # Airtable REST client + retry/cache
│   ├── airtable_watcher.py          # Detect Airtable user-edits via sync_writes
│   ├── sync_engine.py               # Direction 1 — Google → Airtable
│   ├── push_engine.py               # Direction 2 — Airtable → Google
│   ├── delete_engine.py             # Bi-directional delete sync
│   ├── deduplicator.py              # Multi-layer dedup (phone/email/fuzzy)
│   ├── normalizer.py                # E.164 phone + email + name normalization
│   ├── state_manager.py             # SQLite — tokens, cache, logs, write-tracking
│   └── logger.py                    # Daily-rotated file logging
├── fix_names_only.py                # One-shot utility — re-patch names only
├── setup.py                         # Initialize the SQLite database
├── requirements.txt                 # Python dependencies
└── README.md                        # You are here
```

---

## Requirements

- **Python 3.9+**
- **A Google Cloud project** with the **People API** enabled and an **OAuth 2.0 client** (web type)
- **An Airtable base** with the schema described below and a **personal access token**
- Linux/macOS recommended for production (cron); Windows fully supported for development

Python packages (auto-installed via `requirements.txt`):

```
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-auth-httplib2==0.2.0
google-api-python-client==2.127.0
requests==2.31.0
phonenumbers==8.13.37
rapidfuzz==3.9.3
```

---

## Quick start

```bash
git clone https://github.com/AMMorsy/contact-sync.git
cd contact-sync
pip install -r requirements.txt

cp credentials/google_client_secret.example.json credentials/google_client_secret.json
cp config/settings.example.json config/settings.json
# edit both files with your credentials

python setup.py
python src/main.py            # first run — will print an OAuth URL
```

After first authorization, every subsequent run is incremental and silent.

---

## Step-by-step setup

### 1. Clone and install

```bash
git clone https://github.com/AMMorsy/contact-sync.git
cd contact-sync
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create a Google Cloud OAuth client

1. Go to <https://console.cloud.google.com/> → create or select a project.
2. **APIs & Services → Library** → enable **People API**.
3. **APIs & Services → OAuth consent screen** → configure (External, add your email as a test user).
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:8085`
5. Download the JSON, save it as `credentials/google_client_secret.json`.

### 3. Get Airtable credentials

1. Create a base with the [required schema](#required-airtable-schema).
2. Generate a [personal access token](https://airtable.com/create/tokens) with `data.records:read`, `data.records:write`, and `schema.bases:read` scopes, scoped to your base.
3. Note the **base ID** (`appXXXXXXXXXXXXXX`) and **table ID** (`tblXXXXXXXXXXXXXX`) from the [Airtable API docs](https://airtable.com/developers/web/api/introduction) for your base.

### 4. Configure the project

Copy and edit the example files:

```bash
cp credentials/google_client_secret.example.json credentials/google_client_secret.json
cp config/settings.example.json config/settings.json
```

Open `config/settings.json` and fill in:

- `airtable.token` — your personal access token
- `airtable.base_id` and `airtable.table_id`
- `google_accounts[0].email` — the Google account you'll authorize
- `google_accounts[0].redirect_uri` — must match what you set in Google Cloud (default `http://localhost:8085`)
- `sync.default_country_code` — your default country code for local-format numbers (default `+27`)

Paths in the example are **relative to the project root**, so the same config works on any machine. Absolute paths still work if you prefer them.

### 5. Initialize the database

```bash
python setup.py
```

Creates `data/sync_state.db` with all required tables.

### 6. First-run authorization

```bash
python src/main.py
```

The script prints a Google OAuth URL. Open it in a browser, sign in to the configured Google account, approve the scopes, and **paste the full redirect URL back into the terminal**. The token is saved to `config/accounts/account_1.json` and reused for all future runs (auto-refreshing as needed).

The first sync does a full pull and may take a while depending on contact count. Subsequent runs are incremental.

### 7. Schedule automatic sync

**Linux / macOS (cron — every 10 minutes):**

```cron
*/10 * * * * cd /path/to/contact-sync && /usr/bin/python3 src/main.py >> logs/cron.log 2>&1
```

**Windows (Task Scheduler):** create a basic task triggered every 10 minutes that runs:

```
"C:\path\to\python.exe" "C:\path\to\contact-sync\src\main.py"
```

**systemd timer:** see your distro docs — point at the same `python src/main.py` command from the project directory.

---

## Required Airtable schema

Create a single table with these fields (exact field names — they're hard-coded in the sync logic):

| Field name          | Type                  | Notes                                              |
| ------------------- | --------------------- | -------------------------------------------------- |
| First Name          | Single line text      | **Mandatory for push to Google**                   |
| Last Name           | Single line text      |                                                    |
| Full Name           | Formula or text       | `First Name & " " & Last Name`                     |
| Phone               | Phone / text          | Raw phone as Google had it                         |
| Phone 2             | Single line text      | Secondary phone                                    |
| Phone 3             | Single line text      | Tertiary phone                                     |
| Clean Phone         | Single line text      | Digits-only canonical                              |
| Clean Phone 2       | Single line text      |                                                    |
| Dedup Phone         | Single line text      | **Mandatory for push** — E.164 canonical, dedup key |
| All Phones Raw      | Long text             | All raw phone values seen, pipe-separated          |
| Email               | Email                 | Raw email                                          |
| Clean Email         | Email                 | Lowercased canonical                               |
| Company             | Single line text      |                                                    |
| Google Contact ID   | Single line text      | `people/c1234...` — the dedup anchor               |
| Sync Source         | Single line text      | e.g. `Google Account 1`                            |
| Origin              | Single select         | `google_contacts`                                  |
| Last Synced At      | Date with time        | Stamped by the script (UTC)                        |
| Updated At          | Last modified time    | Auto by Airtable — used for conflict tolerance     |
| Sync Lock           | Checkbox              | Set during writes to avoid race conditions         |
| Skip Delete         | Checkbox              | Tick to exempt a row from delete sync              |

> If your column types differ slightly the sync still works — only the **field names** must match.

---

## How the sync works

### Direction 1 — Google → Airtable (`sync_engine.py`)

1. Pull contacts from Google with `syncToken` (incremental). On first run, full pull.
2. Load all Airtable records once (cached for 10 minutes).
3. Build in-memory lookup maps: by Google Contact ID, by phone, by email.
4. For each Google contact:
   - Normalize phones to E.164, split concatenated numbers, dedupe.
   - Skip deleted, no-phone-no-email, or ambiguous (with safety lock) records.
   - Match to existing Airtable row by ID → phone → email (in that order).
   - **New** → create record. **Existing** → patch only changed fields.
5. Save the new `syncToken` for next run.

### Delete sync (`delete_engine.py`)

Runs after Direction 1, before Direction 2. Compares the **current** state of both systems against the `known_records` table.

- In Airtable last run, missing now → user deleted from Airtable → delete from Google.
- In Google last run, missing now → user deleted from Google → delete from Airtable.
- Records with `Skip Delete = true` are **never** deleted.

### Direction 2 — Airtable → Google (`push_engine.py`)

1. The **watcher** (`airtable_watcher.py`) compares each Airtable record's current values against `sync_writes` — the table that records exactly what the script last wrote. If they match, this is a script-write (skip). If they differ, it's a user-edit (push).
2. For each push candidate:
   - Acquire `Sync Lock`.
   - Resolve conflicts: if Google was updated within ±5 min of Airtable, **Google wins**.
   - Create or update the Google contact (with `etag` to avoid conflicts).
   - On 404, recreate the Google contact and store the new ID.
   - Stamp `Last Synced At` (a future timestamp so Airtable's `Updated At` stays earlier).
   - Record the values just sent to Google so the watcher will recognize them next run.

### Loop prevention — the key insight

The watcher does **not** rely on Airtable's `Updated At` field, because writing to Airtable updates that field automatically. Instead, the script tracks the exact field values it last wrote in the `sync_writes` table. A record is only pushed if its current values **differ** from the last script-write — which means a real user edited it.

---

## Configuration reference

`config/settings.json`:

| Key                                        | Default            | Description                                                                  |
| ------------------------------------------ | ------------------ | ---------------------------------------------------------------------------- |
| `airtable.token`                           | —                  | Personal access token                                                        |
| `airtable.base_id`                         | —                  | `appXXXXX...`                                                                |
| `airtable.table_id`                        | —                  | `tblXXXXX...`                                                                |
| `google_accounts[].id`                     | —                  | Internal account identifier (e.g. `account_1`)                               |
| `google_accounts[].email`                  | —                  | The Google email being authorized                                            |
| `google_accounts[].credentials_file`       | relative path      | OAuth client secret JSON                                                     |
| `google_accounts[].token_file`             | relative path      | Where to persist the user's refresh token                                    |
| `google_accounts[].redirect_uri`           | `http://localhost:8085` | Must match the URI registered in Google Cloud                          |
| `google_accounts[].sync_source_label`      | `Google Account 1` | Stamped onto every Airtable record from this account                         |
| `google_accounts[].active`                 | `true`             | Set `false` to skip this account                                             |
| `sync.page_size`                           | `200`              | Google API page size                                                         |
| `sync.airtable_batch_size`                 | `10`               | Airtable batch size                                                          |
| `sync.google_request_delay_seconds`        | `1`                | Pause between Google pages                                                   |
| `sync.airtable_request_delay_seconds`      | `0.25`             | Pause between Airtable requests                                              |
| `sync.max_retries`                         | `5`                | Retry budget for both APIs                                                   |
| `sync.retry_backoff_seconds`               | `2`                | Linear backoff base                                                          |
| `sync.default_country_code`                | `+27`              | Country code for local-format numbers                                        |
| `safety.google_write_enabled`              | `true`             | Master switch for Direction 2 + delete sync                                  |
| `safety.google_delete_enabled`             | `true`             | (reserved) — currently gated by `google_write_enabled`                       |
| `safety.dry_run`                           | `false`            | Log everything, write nothing                                                |

Optional environment overrides:

- `CONTACT_SYNC_DB_PATH` — override the SQLite location
- `CONTACT_SYNC_LOG_DIR` — override the log directory

---

## Operations & troubleshooting

**Inspect the run log:**

```bash
sqlite3 data/sync_state.db "SELECT * FROM run_log ORDER BY id DESC LIMIT 10;"
```

**Tail logs:**

```bash
tail -f logs/sync.log
tail -f logs/errors.log
```

**Force a full re-pull** (rare — use only if you suspect a corrupted sync token):

```bash
sqlite3 data/sync_state.db "DELETE FROM sync_tokens;"
```

**A run got stuck:** the next run automatically kills any prior run older than 20 minutes and removes the stale `data/sync.lock`. The script also self-terminates after 15 minutes via `SIGALRM` (Unix only).

**Duplicates flagged for review:**

```bash
sqlite3 data/sync_state.db "SELECT * FROM flagged_duplicates WHERE resolved = 0;"
```

**Common failures:**

- *"redirect_uri_mismatch"* during OAuth → the URI in `settings.json` doesn't match what you registered in Google Cloud.
- *Airtable 422 "Unknown field name"* → your column names don't match the [schema](#required-airtable-schema).
- *Phones not matching* → check `default_country_code` and the `Dedup Phone` formula.

---

## Utilities

**`fix_names_only.py`** — fast one-shot resync that patches **First Name / Last Name / Full Name** in Airtable from Google, matched by Google Contact ID. Use after manually fixing names in Google when you don't want a full sync run.

```bash
python fix_names_only.py
```

---

## Safety switches

Toggle these in `config/settings.json` whenever you need to test or quarantine the integration:

| Switch                  | Effect when `false`                                            |
| ----------------------- | -------------------------------------------------------------- |
| `safety.dry_run = true` | Logs every intended write but performs **none**                |
| `safety.google_write_enabled = false` | Skips Direction 2 **and** delete sync                |

Recommended first-run setting: `dry_run = true` until the logs look right, then flip to `false`.

---

## Disclaimer

This is a generic open-source contact synchronization tool. It contains **no proprietary credentials, no real user data, and no organization-specific configuration**. All example files use placeholder values. Treat your `config/settings.json` and `credentials/google_client_secret.json` as secrets — they are gitignored by default.

---

## Author

**Ahmed Morsy**

## License

Released under the [MIT License](LICENSE) — © 2026 Ahmed Morsy.

---

Built for engineers who hate duplicate contacts and infinite sync loops.
