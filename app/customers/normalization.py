import re
from typing import Optional


COMPANY_SUFFIXES = (
    r"\bprivate limited\b",
    r"\bpvt\.?\s*ltd\.?\b",
    r"\bpublic limited\b",
    r"\blimited\b",
    r"\bltd\.?\b",
    r"\bllp\b",
    r"\bincorporated\b",
    r"\binc\.?\b",
)

GSTIN_PATTERN = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
)


def clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_email(value: Optional[str]) -> Optional[str]:
    value = clean_optional(value)
    if not value:
        return None
    value = value.lower()
    if value.count("@") != 1:
        return None
    local_part, domain = value.split("@", 1)
    if not local_part or "." not in domain:
        return None
    return f"{local_part}@{domain}"


def normalize_email_domain(value: Optional[str]) -> Optional[str]:
    email = normalize_email(value)
    return email.split("@", 1)[1] if email else None


def normalize_phone(
    value: Optional[str],
    default_country_code: str = "91",
) -> Optional[str]:
    value = clean_optional(value)
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = f"{default_country_code}{digits}"
    return digits if 10 <= len(digits) <= 15 else None


def normalize_gstin(value: Optional[str]) -> Optional[str]:
    value = clean_optional(value)
    if not value:
        return None
    normalized = re.sub(r"\s+", "", value).upper()
    return normalized if GSTIN_PATTERN.fullmatch(normalized) else None


def normalize_company_name(value: Optional[str]) -> Optional[str]:
    value = clean_optional(value)
    if not value:
        return None
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    for suffix_pattern in COMPANY_SUFFIXES:
        normalized = re.sub(suffix_pattern, " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized or None
