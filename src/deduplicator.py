import re
from rapidfuzz import fuzz
from state_manager import (
    find_in_cache_by_phone,
    find_in_cache_by_email,
    flag_duplicate
)
from logger import get_logger

logger = get_logger()

FUZZY_NAME_THRESHOLD = 85

def is_email_as_name(name):
    """Detect contacts where the name is actually an email address or domain."""
    if not name:
        return True
    if "@" in name:
        return True
    if re.match(r'^[a-zA-Z0-9._%+\-]+\.[a-zA-Z]{2,}$', name.strip()):
        return True
    return False

def check_duplicate(contact, airtable_client):
    dedup_phone = contact.get("dedup_phone")
    canonical_email = contact.get("clean_email")
    incoming_name = contact.get("full_name", "")

    # Layer 1: exact phone match in local cache
    if dedup_phone:
        cached = find_in_cache_by_phone(dedup_phone)
        if cached:
            logger.debug(f"Cache hit (phone): {dedup_phone}")
            return "existing", cached["airtable_record_id"], "exact_phone_cache"

    # Layer 2: exact email match in local cache
    if canonical_email:
        cached = find_in_cache_by_email(canonical_email)
        if cached:
            logger.debug(f"Cache hit (email): {canonical_email}")
            return "existing", cached["airtable_record_id"], "exact_email_cache"

    # Layer 3: check Airtable directly by phone
    if dedup_phone:
        results = airtable_client.search_by_field("Dedup Phone", dedup_phone)
        if results:
            record = results[0]
            return "existing", record["id"], "exact_phone_airtable"

    # Layer 4: check Airtable directly by email
    if canonical_email:
        results = airtable_client.search_by_field("Clean Email", canonical_email)
        if results:
            record = results[0]
            airtable_name = record.get("fields", {}).get("Full Name", "")

            # Skip fuzzy check if name looks like an email/domain
            if is_email_as_name(incoming_name) or is_email_as_name(airtable_name):
                return "existing", record["id"], "exact_email_airtable"

            if airtable_name and incoming_name:
                score = fuzz.token_sort_ratio(
                    incoming_name.lower(), airtable_name.lower()
                )
                if score >= FUZZY_NAME_THRESHOLD:
                    logger.info(f"Fuzzy match (email+name {score}%): {incoming_name} ~ {airtable_name}")
                    flag_duplicate(
                        dedup_phone, canonical_email, incoming_name,
                        record["id"], "fuzzy_email_name"
                    )
                    return "flagged", record["id"], "fuzzy_email_name"

            return "existing", record["id"], "exact_email_airtable"

    return "new", None, None
