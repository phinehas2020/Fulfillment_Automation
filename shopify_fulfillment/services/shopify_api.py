"""Shopify Admin API client placeholder."""

from typing import Any, Dict


class ShopifyAPI:
    """Wraps Shopify Admin API calls. Implementation deferred."""

    def __init__(self, shop_domain: str, api_key: str, api_version: str):
        self.shop_domain = shop_domain
        self.api_key = api_key
        self.api_version = api_version

    def get_shipping_rates(self, order: Dict[str, Any]):
        raise NotImplementedError("Rate shopping not yet implemented.")

    def purchase_label(self, order: Dict[str, Any], rate_id: str):
        raise NotImplementedError("Label purchase not yet implemented.")

    def create_fulfillment(self, order: Dict[str, Any], tracking_info: Dict[str, Any]):
        raise NotImplementedError("Fulfillment creation not yet implemented.")

    @staticmethod
    def validate_webhook(payload: bytes, signature: str, secret: str) -> bool:
        raise NotImplementedError("HMAC validation not yet implemented.")


