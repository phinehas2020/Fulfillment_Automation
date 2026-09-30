"""Compare submitted and carrier-validated destinations before buying postage."""

import hashlib
import json
import re
import unicodedata


ADDRESS_FIELDS = ("street1", "street2", "city", "state", "zip", "country")


def address_snapshot(address):
    """Keep only destination fields, never contact details or API metadata."""
    address = address or {}
    return {field: str(address.get(field) or "").strip() for field in ADDRESS_FIELDS}


def _text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[\w]+", value))


def _zip(value, country):
    compact = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    if _text(country) in ("us", "usa", "united states") and re.fullmatch(r"\d{5}(?:\d{4})?", compact):
        return compact[:5]  # USPS ZIP+4 does not change the delivery ZIP.
    return compact


def normalized_destination(address):
    address = address_snapshot(address)
    return (
        _text(" ".join((address["street1"], address["street2"]))),
        _text(address["city"]),
        _text(address["state"]),
        _zip(address["zip"], address["country"]),
        _text(address["country"]),
    )


def material_address_changes(submitted, validated, validation_results=None):
    """Return changed delivery components; fail closed if no verified address."""
    if not isinstance(validated, dict) or not any(validated.get(key) for key in ADDRESS_FIELDS):
        return ["validated destination unavailable"]
    if isinstance(validation_results, dict) and validation_results.get("is_valid") is False:
        return ["destination failed validation"]
    names = ("street", "city", "state", "ZIP", "country")
    old = normalized_destination(submitted)
    new = normalized_destination(validated)
    return [name for name, before, after in zip(names, old, new) if before != after]


def address_review_fingerprint(submitted, validated):
    pair = [normalized_destination(submitted), normalized_destination(validated)]
    return hashlib.sha256(json.dumps(pair, separators=(",", ":")).encode()).hexdigest()


def display_address(address):
    address = address_snapshot(address)
    return "\n".join(filter(None, (
        address["street1"], address["street2"],
        ", ".join(filter(None, (address["city"], address["state"], address["zip"]))),
        address["country"],
    )))
