"""Pure validation for the versioned label-packing workflow.

Tracking identities come from exact saved tracking numbers or exact barcode
fields in the purchased label. No substring/fuzzy customer matching is allowed.
"""
import hashlib
import json
import re
import uuid
from collections import Counter

from odoo.exceptions import UserError

# Trusted in-process bridge marker. A JSON-RPC context cannot represent this
# object identity, so callers cannot forge authenticated phone provenance.
PACKING_MOBILE_REQUEST = object()


class PackingError(UserError):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def canonical_barcode(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise PackingError("invalid_barcode", "Enter or scan a valid shipping tracking barcode.", 422)
    # Spaces are a human-readable presentation convention; preserve every digit.
    # GS1 scanners may expose the AIM ]C1 symbology identifier and ASCII GS.
    # These are encoding metadata; the full carrier data remains unchanged.
    if value.startswith("]C1"):
        value = value[3:]
    result = re.sub(r"[\s\x1d]", "", value).upper()
    if not re.fullmatch(r"[A-Z0-9]+", result):
        raise PackingError("unsupported_barcode", "This barcode format is not supported; enter the tracking number.", 422)
    return result


def saved_label_barcodes(zpl, tracking):
    """Allow only whole Code 128 fields from the purchased label tied to tracking.

    Zebra's >; and >: are Code 128 start-code invocation sequences, not data.
    >8 is FNC1, represented by ASCII GS in scanner output. Other invocation
    sequences remain unsupported rather than guessed. See Zebra's ^BC guide.
    """
    tracking = canonical_barcode(tracking)
    result = {tracking}
    for field in re.findall(r"\^BC[^\^]*((?:(?!\^BC|\^FS).)*)\^FS", zpl or "", re.S):
        match = re.search(r"\^F[DV]([^\^]*)", field)
        if not match:
            continue
        value = match.group(1)
        if value.startswith((">;", ">:")):
            value = value[2:]
        value = value.replace(">8", "\x1d")
        try:
            value = canonical_barcode(value)
        except PackingError:
            continue
        # A whole saved carrier barcode ending in its exact tracking identity is
        # verifiable; any other barcode on the same label is unrelated.
        if value == tracking or value.endswith(tracking):
            result.add(value)
    return sorted(result)


def quantity_map(value, allow_zero=False):
    try:
        mapping = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise PackingError("invalid_allocations", "This box needs its item allocations reviewed.") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise PackingError("invalid_allocations", "This box has no verified item allocations.")
    result = {}
    for key, quantity in mapping.items():
        if isinstance(key, bool) or not re.fullmatch(r"[1-9][0-9]*", str(key)):
            raise PackingError("invalid_allocations", "An item allocation has an invalid line identity.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < (0 if allow_zero else 1):
            raise PackingError("invalid_allocations", "Item quantities must be whole units.")
        numeric_key = int(key)
        if numeric_key in result:
            raise PackingError("invalid_allocations", "This box contains a duplicate item identity.")
        result[numeric_key] = quantity
    return result


def validate_allocations(expected, boxes):
    totals = Counter()
    if not expected or not boxes:
        raise PackingError("invalid_allocations", "The order needs physical items and purchased boxes.")
    for mapping in boxes:
        if not mapping or any(line_id not in expected for line_id in mapping):
            raise PackingError("invalid_allocations", "A box references an item outside this order.")
        totals.update(mapping)
    if dict(totals) != expected:
        raise PackingError("allocation_mismatch", "Box quantities do not exactly match the order. Supervisor review is required.")
    return True


def event_identity(event_id, device_id):
    try:
        value = str(uuid.UUID(str(event_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PackingError("invalid_event", "The action needs a valid unique event identity.", 422) from exc
    if value != str(event_id).lower():
        raise PackingError("invalid_event", "The event identity must be a canonical UUID.", 422)
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise PackingError("invalid_device", "The action needs an authenticated device.", 422)
    return value, device_id


def payload_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fulfillment_matches(fulfillment, tracking, line_items):
    if fulfillment.get("status") not in ("success", "open", "pending"):
        return False
    numbers = fulfillment.get("tracking_numbers") or [fulfillment.get("tracking_number")]
    try:
        matched_tracking = any(number and canonical_barcode(str(number)) == canonical_barcode(tracking) for number in numbers)
    except PackingError:
        return False
    if not matched_tracking:
        return False
    actual = Counter()
    for line in fulfillment.get("line_items") or []:
        actual[str(line.get("id"))] += int(line.get("quantity") or 0)
    expected = Counter()
    for line in line_items:
        expected[str(line["shopify_line_id"])] += line["quantity"]
    return dict(actual) == dict(expected)
