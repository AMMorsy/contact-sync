import time
from googleapiclient.discovery import build
from logger import get_logger

logger = get_logger()

PERSON_FIELDS = "names,phoneNumbers,emailAddresses,organizations,metadata"

def build_service(creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("people", "v1", http=http, cache_discovery=False)

def fetch_all_contacts(service, account_id, sync_token=None, page_size=200, delay=1):
    contacts = []
    next_page_token = None
    new_sync_token = None
    page_num = 0
    while True:
        page_num += 1
        params = {
            "resourceName": "people/me",
            "pageSize": page_size,
            "personFields": PERSON_FIELDS,
            "requestSyncToken": True
        }
        if sync_token and not next_page_token:
            params["syncToken"] = sync_token
        if next_page_token:
            params["pageToken"] = next_page_token
        try:
            result = service.people().connections().list(**params).execute()
        except Exception as e:
            error_msg = str(e)
            if "Sync token" in error_msg or "invalid" in error_msg.lower():
                logger.warning(f"[{account_id}] Sync token expired. Falling back to full pull.")
                params.pop("syncToken", None)
                result = service.people().connections().list(**params).execute()
            else:
                raise
        page_contacts = result.get("connections", [])
        contacts.extend(page_contacts)
        new_sync_token = result.get("nextSyncToken", new_sync_token)
        next_page_token = result.get("nextPageToken")
        logger.info(f"[{account_id}] Page {page_num}: {len(page_contacts)} contacts pulled")
        if not next_page_token:
            break
        time.sleep(delay)
    logger.info(f"[{account_id}] Total pulled: {len(contacts)}")
    return contacts, new_sync_token
