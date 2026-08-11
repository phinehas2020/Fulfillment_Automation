"""Pure helpers for classifying Shopify pickup orders."""

import re
from typing import Dict, Iterable, List


PICKUP_METHOD_TYPES = {"pickup", "retail", "local_pickup"}


def normalize_fulfillment_text(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def shipping_line_texts(payload: Dict) -> List[str]:
    texts = []
    for line in (payload or {}).get("shipping_lines") or []:
        for key in ("title", "code", "carrier_identifier"):
            normalized = normalize_fulfillment_text(line.get(key))
            if normalized:
                texts.append(normalized)
    return texts


def payload_is_pickup(payload: Dict) -> bool:
    """Return True only for an explicit pickup shipping method."""
    return any(
        "pickup" in text or "pick up" in text
        for text in shipping_line_texts(payload)
    )


def payload_has_ambiguous_physical_fulfillment(payload: Dict) -> bool:
    """Flag physical Shopify orders with neither an address nor explicit pickup."""
    payload = payload or {}
    physical_lines = [
        line
        for line in payload.get("line_items") or []
        if line.get("requires_shipping", True)
    ]
    return bool(
        physical_lines
        and not payload.get("shipping_address")
        and not payload_is_pickup(payload)
    )


def fulfillment_orders_confirm_pickup(method_types: Iterable[str]):
    """Return True/False when Shopify has delivery methods, otherwise None."""
    normalized = {
        normalize_fulfillment_text(method_type).replace(" ", "_")
        for method_type in (method_types or [])
        if method_type
    }
    if not normalized:
        return None
    return bool(normalized & PICKUP_METHOD_TYPES)
