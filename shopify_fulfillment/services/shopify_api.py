"""Shopify Admin API helper."""

import base64
import hmac
import logging
from hashlib import sha256
from typing import Any, Dict, List, Optional

import requests
from odoo import exceptions

_logger = logging.getLogger(__name__)


class ShopifyAPI:
    """Thin wrapper around Shopify Admin API."""

    def __init__(self, shop_domain: str, api_key: str, api_version: str, webhook_secret: Optional[str] = None):
        self.shop_domain = shop_domain
        self.api_key = api_key
        self.api_version = api_version
        self.webhook_secret = webhook_secret

    @classmethod
    def from_env(cls, env):
        ICP = env["ir.config_parameter"].sudo()
        shop_domain = ICP.get_param("shopify.shop_domain")
        api_key = ICP.get_param("shopify.api_key")
        api_version = ICP.get_param("shopify.api_version") or "2024-01"
        webhook_secret = ICP.get_param("shopify.webhook_secret")
        if not shop_domain or not api_key:
            raise exceptions.UserError("Shopify domain/api key not configured")
        return cls(shop_domain, api_key, api_version, webhook_secret)

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.api_key,
        }

    def _url(self, path: str) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}{path}"

    def get_shipping_rates(self, order) -> List[Dict[str, Any]]:
        """
        Placeholder: Shopify Shipping Rates API depends on fulfillment orders.
        Here we return an empty list; implement real calls as needed.
        """
        _logger.info("Rate shopping for order %s - placeholder rate", order.id)
        return [{"id": "placeholder_rate", "amount": 0.0, "currency": "USD", "service": "Ground"}]

    def purchase_label(self, order, rate_id: str) -> Dict[str, Any]:
        """
        Placeholder label purchase. Replace with real Shopify Shipping Labels API call.
        """
        _logger.info("Purchasing label for order %s (rate %s) - placeholder", order.id, rate_id)
        # Provide minimal stub so downstream flow can proceed
        dummy_tracking = f"TRACK-{order.id}"
        return {
            "carrier": "TestCarrier",
            "service": "Ground",
            "tracking_number": dummy_tracking,
            "tracking_url": f"https://example.com/track/{dummy_tracking}",
            "label_url": "",
            "label_zpl": "^XA^FO50,50^ADN,36,20^FDTest Label^FS^XZ",
            "rate_amount": 0.0,
            "rate_currency": "USD",
            "shopify_fulfillment_id": "",
        }

    def create_fulfillment(self, order, tracking_info: Dict[str, Any]):
        """
        Basic fulfillment creation using the Fulfillment API.
        """
        fulfillment = {
            "fulfillment": {
                "location_id": None,
                "tracking_number": tracking_info.get("tracking_number"),
                "tracking_url": tracking_info.get("tracking_url"),
                "notify_customer": True,
                "line_items_by_fulfillment_order": [],
            }
        }
        url = self._url(f"/orders/{order.shopify_id}/fulfillments.json")
        resp = requests.post(url, headers=self._headers(), json=fulfillment, timeout=30)
        if resp.status_code >= 400:
            raise exceptions.UserError(f"Fulfillment creation failed: {resp.text}")
        return resp.json()

    @staticmethod
    def validate_webhook(payload: bytes, signature: str, secret: str) -> bool:
        digest = hmac.new(secret.encode(), payload, sha256).digest()
        computed = base64.b64encode(digest).decode()
        return signature and hmac.compare_digest(computed, signature)



