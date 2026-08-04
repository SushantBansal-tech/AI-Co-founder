import re


PRODUCT_CODE_PATTERN = re.compile(r"\b([A-Z]{2,8})[\s_-]?(\d{2,8})\b", re.IGNORECASE)
GRADE_PATTERNS = (
    re.compile(r"\bE\s*[- ]?(250|350|410)\b", re.IGNORECASE),
    re.compile(r"\bFE\s*[- ]?(415|500D?|550D?|600)\b", re.IGNORECASE),
    re.compile(r"\bSS\s*[- ]?(304L?|316L?)\b", re.IGNORECASE),
)
STANDARD_PATTERN = re.compile(r"\b(?:IS|ASTM|EN)\s*[- ]?([A-Z0-9.]+)\b", re.IGNORECASE)
SIZE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*(MM|NB|INCH|INCHES|\")\b", re.IGNORECASE)
PIPE_CLASS_PATTERN = re.compile(r"\b(LIGHT|MEDIUM|HEAVY)\s*(?:CLASS)?\b", re.IGNORECASE)


def normalize_product_code(value: str | None) -> str | None:
    if not value:
        return None
    match = PRODUCT_CODE_PATTERN.search(value.upper())
    return f"{match.group(1).upper()}-{match.group(2)}" if match else None


def _normalized_grade(text: str) -> str | None:
    for pattern in GRADE_PATTERNS:
        match = pattern.search(text)
        if match:
            prefix = pattern.pattern.split("\\s")[0].replace("\\b", "")
            if prefix == "E":
                return f"E{match.group(1).upper()}"
            if prefix == "FE":
                return f"FE{match.group(1).upper()}"
            return f"SS{match.group(1).upper()}"
    return None


def normalize_requirement(extraction: dict, raw_text: str = "") -> dict:
    text = " ".join(str(value or "") for value in (
        extraction.get("product_requested"),
        extraction.get("specifications"),
        raw_text,
    )).upper()
    standard = STANDARD_PATTERN.search(text)
    size = SIZE_PATTERN.search(text)
    pipe_class = PIPE_CLASS_PATTERN.search(text)
    return {
        "raw_product_text": extraction.get("product_requested") or "",
        "product_code": normalize_product_code(text),
        "grade": _normalized_grade(text),
        "standard": (
            f"{standard.group(0).replace(' ', '').replace('-', '').upper()}"
            if standard else None
        ),
        "size_value": float(size.group(1)) if size else None,
        "size_unit": (
            "INCH" if size and size.group(2).upper() in {'INCH', 'INCHES', '"'}
            else size.group(2).upper() if size else None
        ),
        "pipe_class": pipe_class.group(1).upper() if pipe_class else None,
        "quantity": extraction.get("quantity"),
        "normalization_warnings": [],
    }


def _compact(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9.]", "", (value or "").upper())


def verify_structured_product(normalized: dict, product) -> dict:
    evidence = " ".join((product.name or "", product.grade or "", product.specifications or ""))
    evidence_compact = _compact(evidence)
    mismatches = []
    for field in ("grade", "standard"):
        requested = normalized.get(field)
        if requested and _compact(str(requested)) not in evidence_compact:
            mismatches.append({
                "field": field,
                "requested": requested,
                "catalog_value": product.grade if field == "grade" else product.specifications,
                "critical": True,
            })
    requested_class = normalized.get("pipe_class")
    if requested_class and _compact(requested_class) not in evidence_compact:
        mismatches.append({
            "field": "pipe_class",
            "requested": requested_class,
            "catalog_value": product.specifications,
            "critical": True,
        })
    # Size ranges are not guessed. Presence is verified; range interpretation
    # remains a technical-review concern when the catalog is unstructured text.
    size = normalized.get("size_value")
    unit = normalized.get("size_unit")
    if size is not None and _compact(f"{size:g}{unit or ''}") not in evidence_compact:
        range_numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", evidence)]
        if not range_numbers or not (min(range_numbers) <= size <= max(range_numbers)):
            mismatches.append({
                "field": "size",
                "requested": f"{size:g}{unit or ''}",
                "catalog_value": product.specifications,
                "critical": True,
            })
    return {
        "exact": not mismatches,
        "mismatches": mismatches,
        "requires_technical_review": bool(mismatches),
    }
