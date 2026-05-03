import re
import phonenumbers
from logger import get_logger

logger = get_logger()

DEFAULT_COUNTRY_CODE = "+27"


def _normalize_single_phone(raw):
    """
    Normalize one raw phone string to E.164.

    Rule:
    - Less than 11 digits → South African local → add +27
    - 11 or more digits → has country code → parse directly
    - If cannot parse → invalid
    - If genuinely ambiguous → ambiguous (rare)

    Returns: (canonical, clean, status)
    Status: 'ok' | 'ambiguous' | 'invalid'
    """
    if not raw:
        return None, None, "invalid"

    raw = str(raw).strip()

    # Replace leading 00 with +
    if re.match(r'^00\d', raw):
        raw = "+" + raw[2:]

    has_plus = raw.startswith("+")
    digits = re.sub(r"[^\d]", "", raw)

    if not digits or len(digits) < 4:
        return None, None, "invalid"

    # ── Less than 11 digits → South African local ────────────────
    if len(digits) < 11:
        national = digits.lstrip("0") or digits
        candidate = DEFAULT_COUNTRY_CODE + national
        try:
            parsed = phonenumbers.parse(candidate, None)
            if phonenumbers.is_valid_number(parsed):
                canonical = phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
                return canonical, re.sub(r"[^\d]", "", canonical), "ok"
        except Exception:
            pass
        return None, None, "invalid"

    # ── 11 or more digits → must contain country code ────────────
    # Try with + prefix (standard E.164)
    to_parse = ("+" if not has_plus else "") + digits if not has_plus else raw
    try:
        parsed = phonenumbers.parse(to_parse, None)
        if phonenumbers.is_valid_number(parsed):
            canonical = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
            return canonical, re.sub(r"[^\d]", "", canonical), "ok"
    except Exception:
        pass

    # Try adding + if not present
    if not has_plus:
        try:
            parsed = phonenumbers.parse("+" + digits, None)
            if phonenumbers.is_valid_number(parsed):
                canonical = phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
                return canonical, re.sub(r"[^\d]", "", canonical), "ok"
        except Exception:
            pass

    # Could not parse → invalid (do NOT loop all regions)
    logger.debug(f"Could not normalize phone: {raw}")
    return None, None, "invalid"


def _split_concatenated_phones(raw):
    """
    Split a string containing multiple concatenated phone numbers.
    Handles: :::, ::, ;, comma, slash, plus-sign boundaries, length-based.
    """
    if not raw:
        return []

    raw = str(raw).strip()

    # Step 1: Split on known separators
    parts = re.split(r":::?|;|,|\s{2,}|\s*/\s*", raw)
    parts = [p.strip() for p in parts if p.strip()]

    # Step 2: Split on embedded + signs after position 0
    result = []
    for part in parts:
        plus_positions = [i for i, c in enumerate(part) if c == "+" and i > 0]
        if plus_positions:
            chunks = []
            prev = 0
            for pos in plus_positions:
                chunks.append(part[prev:pos])
                prev = pos
            chunks.append(part[prev:])
            result.extend([c.strip() for c in chunks if c.strip()])
        else:
            result.append(part)

    # Step 3: Length-based split for digit strings > 15 digits
    final = []
    for part in result:
        digits = re.sub(r"[^\d]", "", part)
        if len(digits) > 15:
            split_results = _length_split(digits)
            if split_results:
                final.extend(split_results)
            else:
                final.append(part)
        else:
            final.append(part)

    # Step 4: Deduplicate by digit content
    seen_digits = set()
    unique = []
    for item in final:
        d = re.sub(r"[^\d]", "", item)
        if d and d not in seen_digits:
            seen_digits.add(d)
            unique.append(item)

    return unique


def _length_split(digits):
    """
    Split a digit string too long for one number.
    Returns first valid pair where both parts normalize correctly.
    Prefers two international numbers (11+ digits each).
    """
    candidates = []

    for split_at in range(7, len(digits) - 6):
        part1 = digits[:split_at]
        part2 = digits[split_at:]

        c1, _, s1 = _normalize_single_phone(part1)
        c2, _, s2 = _normalize_single_phone(part2)

        if s1 == "ok" and s2 == "ok":
            candidates.append((c1, c2))

    if not candidates:
        return []

    # Prefer both international
    for c1, c2 in candidates:
        if len(re.sub(r"[^\d]", "", c1)) >= 11 and \
           len(re.sub(r"[^\d]", "", c2)) >= 11:
            return [c1, c2]

    return [candidates[0][0], candidates[0][1]]


def extract_all_phones(phone_values):
    """
    Process all raw phone values from Google API.
    Returns:
        phone1_canonical, phone1_clean,
        phone2_canonical, phone3_canonical,
        all_phones_raw, is_ambiguous
    """
    all_raw_parts = []
    normalized = []
    seen = set()
    is_ambiguous = False

    for raw_value in phone_values:
        if not raw_value:
            continue
        parts = _split_concatenated_phones(str(raw_value))
        for part in parts:
            all_raw_parts.append(part)
            canonical, clean, status = _normalize_single_phone(part)

            if status == "ambiguous":
                is_ambiguous = True
                logger.warning(f"Ambiguous phone: {part}")

            if status == "ok" and canonical and canonical not in seen:
                seen.add(canonical)
                normalized.append((canonical, clean))

    phone1_canonical = normalized[0][0] if len(normalized) > 0 else None
    phone1_clean     = normalized[0][1] if len(normalized) > 0 else None
    phone2_canonical = normalized[1][0] if len(normalized) > 1 else None
    phone3_canonical = normalized[2][0] if len(normalized) > 2 else None
    all_raw = " | ".join(all_raw_parts) if all_raw_parts else None

    return phone1_canonical, phone1_clean, phone2_canonical, phone3_canonical, all_raw, is_ambiguous


def normalize_email(raw_email):
    if not raw_email:
        return None
    raw = str(raw_email).strip().lower()
    parts = raw.split()
    valid = None
    for part in parts:
        if "@" in part and "." in part.split("@")[-1]:
            if len(part) > (len(valid) if valid else 0):
                valid = part
    return valid


def normalize_name(first, last, display):
    """Names taken AS-IS from Google — no cleaning, no title case, no symbol removal.
    Special characters like (), brackets, dots are preserved exactly."""
    first   = (str(first).strip() if first else "")
    last    = (str(last).strip() if last else "")
    display = (str(display).strip() if display else "")

    if not first and not last:
        if display:
            parts = display.split()
            if len(parts) >= 2:
                first = parts[0]
                last  = " ".join(parts[1:])
            else:
                first = display
                last  = ""

    full = f"{first} {last}".strip()
    if not full:
        full = display
    return first, last, full


def normalize_contact(raw_contact):
    names         = raw_contact.get("names", [{}])
    name_obj      = names[0] if names else {}
    phone_entries = raw_contact.get("phoneNumbers", [])
    emails        = raw_contact.get("emailAddresses", [])
    orgs          = raw_contact.get("organizations", [])

    phone_values = [p.get("value") for p in phone_entries if p.get("value")]

    (dedup_phone, clean_phone,
     phone2, phone3,
     all_phones_raw, is_ambiguous) = extract_all_phones(phone_values)

    raw_email   = emails[0].get("value") if emails else None
    raw_first   = name_obj.get("givenName", "")
    raw_last    = name_obj.get("familyName", "")
    raw_display = name_obj.get("displayName", "")
    raw_org     = orgs[0].get("name", "") if orgs else ""

    canonical_email = normalize_email(raw_email)
    first_name, last_name, full_name = normalize_name(
        raw_first, raw_last, raw_display
    )
    deleted = raw_contact.get("metadata", {}).get("deleted", False)

    return {
        "google_contact_id": raw_contact.get("resourceName", ""),
        "first_name":        first_name,
        "last_name":         last_name,
        "full_name":         full_name,
        "raw_phone":         phone_values[0] if phone_values else None,
        "clean_phone":       clean_phone,
        "dedup_phone":       dedup_phone,
        "phone2":            phone2,
        "phone3":            phone3,
        "all_phones_raw":    all_phones_raw,
        "raw_email":         raw_email,
        "clean_email":       canonical_email,
        "company":           raw_org.strip().title() if raw_org else "",
        "deleted":           deleted,
        "has_phone":         dedup_phone is not None,
        "has_email":         canonical_email is not None,
        "is_ambiguous":      is_ambiguous
    }
