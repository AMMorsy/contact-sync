import time
from googleapiclient.discovery import build
from logger import get_logger

logger = get_logger()


def build_service(creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("people", "v1", http=http, cache_discovery=False)


def _build_person_body(record_fields):
    """Build Google People API body using ONLY:
    First Name (mandatory), Last Name (optional),
    Dedup Phone (mandatory), Email (optional).
    Names taken as-is — no normalization, no title case.
    Full Name field is IGNORED."""
    body = {}

    first = (record_fields.get("First Name") or "").strip()
    last  = (record_fields.get("Last Name") or "").strip()

    if first:
        display = f"{first} {last}".strip()
        body["names"] = [{
            "givenName": first,
            "familyName": last,
            "displayName": display
        }]

    phone = (record_fields.get("Dedup Phone") or "").strip()
    if phone:
        body["phoneNumbers"] = [{"value": phone, "type": "mobile"}]

    # Email field — fall back to Clean Email if Email is empty
    email = (record_fields.get("Email") or "").strip()
    if not email:
        email = (record_fields.get("Clean Email") or "").strip()
    if email:
        body["emailAddresses"] = [{"value": email, "type": "work"}]

    return body


def create_contact(service, record_fields, dry_run=False):
    """Create new Google contact. Requires First Name + Dedup Phone."""
    first = (record_fields.get("First Name") or "").strip()
    phone = (record_fields.get("Dedup Phone") or "").strip()
    if not first or not phone:
        logger.warning(
            f"Skipping create — First Name and Dedup Phone are mandatory. "
            f"Got: first='{first}' phone='{phone}'"
        )
        return None

    body = _build_person_body(record_fields)
    display_name = body.get("names", [{}])[0].get("displayName", "?")

    logger.info(f"[GOOGLE WRITE] CREATE: {display_name} | {phone}")

    if dry_run:
        logger.info("[DRY RUN] Would have created contact in Google")
        return None

    try:
        result = service.people().createContact(body=body).execute()
        resource_name = result.get("resourceName")
        logger.info(f"[GOOGLE WRITE] Created successfully: {resource_name}")
        time.sleep(1)
        return resource_name
    except Exception as e:
        logger.error(f"[GOOGLE WRITE] Create failed for {display_name}: {e}")
        return None


def update_contact(service, resource_name, record_fields, dry_run=False):
    """Update existing Google contact. Uses field mask — only sends provided fields.
    Fetches current etag before updating to avoid conflicts."""
    body = _build_person_body(record_fields)

    if not body:
        logger.info(f"Nothing to update for {resource_name}")
        return True

    update_fields = []
    if "names" in body:
        update_fields.append("names")
    if "phoneNumbers" in body:
        update_fields.append("phoneNumbers")
    if "emailAddresses" in body:
        update_fields.append("emailAddresses")

    if not update_fields:
        return True

    update_mask = ",".join(update_fields)
    display_name = body.get("names", [{}])[0].get("displayName", "?")

    logger.info(f"[GOOGLE WRITE] UPDATE: {resource_name} | fields: {update_mask} | {display_name}")

    if dry_run:
        return True

    try:
        # Fetch current etag to avoid mismatch
        current = service.people().get(
            resourceName=resource_name,
            personFields="names,phoneNumbers,emailAddresses"
        ).execute()
        body["etag"] = current.get("etag", "*")

        service.people().updateContact(
            resourceName=resource_name,
            updatePersonFields=update_mask,
            body=body
        ).execute()
        logger.info(f"[GOOGLE WRITE] Updated successfully: {resource_name}")
        time.sleep(1)
        return True
    except Exception as e:
        error_str = str(e)
        if "404" in error_str or "notFound" in error_str:
            logger.warning(f"[GOOGLE WRITE] Contact not found: {resource_name} — will recreate")
            return "not_found"
        logger.error(f"[GOOGLE WRITE] Update failed for {resource_name}: {e}")
        return False


def delete_contact(service, resource_name, dry_run=False):
    """Delete a contact from Google. Use ONLY when confirmed deleted in Airtable.
    Returns True on success, False on failure."""
    if not resource_name:
        return False

    logger.info(f"[GOOGLE WRITE] DELETE: {resource_name}")

    if dry_run:
        logger.info("[DRY RUN] Would have deleted contact in Google")
        return True

    try:
        service.people().deleteContact(resourceName=resource_name).execute()
        logger.info(f"[GOOGLE WRITE] Deleted successfully: {resource_name}")
        time.sleep(1)
        return True
    except Exception as e:
        error_str = str(e)
        if "404" in error_str or "notFound" in error_str:
            logger.info(f"[GOOGLE WRITE] Already gone: {resource_name}")
            return True
        logger.error(f"[GOOGLE WRITE] Delete failed for {resource_name}: {e}")
        return False
