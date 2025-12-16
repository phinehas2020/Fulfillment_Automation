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
        Fetch rates from external carrier or Shopify.
        Currently stubbed as we need carrier credentials (UPS/FedEx/EasyPost) to get real rates.
        """
        _logger.info("Rate shopping for order %s", order.id)
        # TODO: Integrate with EasyPost or Shippo here.
        return [
            {"id": "ground", "amount": 12.50, "currency": "USD", "service": "Ground", "carrier": "UPS"},
            {"id": "priority", "amount": 18.00, "currency": "USD", "service": "Priority", "carrier": "UPS"},
        ]

    def purchase_label(self, order, rate_id: str) -> Dict[str, Any]:
        """
        Purchase a label. Currently generates a mock ZPL label.
        """
        _logger.info("Purchasing label for order %s (rate %s)", order.id, rate_id)
        
        # Mock tracking number
        import random
        tracking_num = f"1Z{random.randint(100000, 999999)}"
        
        # Generate a simple ZPL label for testing
        zpl = f"""
^XA
^PW812
^LL1218
^FO50,50^ADN,36,20^FD{order.order_name}^FS
^FO50,100^ADN,36,20^FDShip To: {order.customer_name}^FS
^FO50,150^ADN,36,20^FD{order.shipping_city}, {order.shipping_state}^FS
^FO50,250^BY3
^BCN,100,Y,N,N
^FD{tracking_num}^FS
^XZ
"""
        return {
            "carrier": "UPS",
            "service": "Ground",
            "tracking_number": tracking_num,
            "tracking_url": f"https://www.ups.com/track?loc=en_US&tracknum={tracking_num}",
            "label_url": "",
            "label_zpl": zpl.strip(),
            "rate_amount": 12.50,
            "rate_currency": "USD",
            "shopify_fulfillment_id": "",
        }

    def create_fulfillment(self, order, tracking_info: Dict[str, Any]):
        """
        Create a fulfillment in Shopify using the Fulfillment Orders API.
        """
        # 1. Get Fulfillment Order ID
        fo_id = self._get_fulfillment_order_id(order.shopify_id)
        if not fo_id:
            raise exceptions.UserError("No open fulfillment order found in Shopify.")

        # 2. Create Fulfillment
        payload = {
            "fulfillment": {
                "line_items_by_fulfillment_order": [
                    {
                        "fulfillment_order_id": fo_id,
                        # Fulfill all open items by default
                    }
                ],
                "tracking_info": {
                    "number": tracking_info.get("tracking_number"),
                    "url": tracking_info.get("tracking_url"),
                    "company": tracking_info.get("carrier", "Other"),
                },
                "notify_customer": True,
            }
        }
        
        url = self._url("/fulfillments.json")
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
        if resp.status_code >= 400:
            raise exceptions.UserError(f"Fulfillment failed: {resp.text}")
        return resp.json()

    def _get_fulfillment_order_id(self, shopify_order_id: str) -> Optional[int]:
        """Fetch the first open fulfillment order ID for this order."""
        url = self._url(f"/orders/{shopify_order_id}/fulfillment_orders.json")
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code != 200:
            _logger.error("Failed to fetch fulfillment orders: %s", resp.text)
            return None
        
        data = resp.json()
        for fo in data.get("fulfillment_orders", []):
            if fo.get("status") == "open":
                return fo.get("id")
        return None

    def get_orders(self, shopify_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch multiple orders by Shopify ID.
        """
        if not shopify_ids:
            return []
            
        # Shopify allows fetching by IDs using comma-separated list
        # We need to chunk it if it's too large, but for now we assume < 50 items
        ids_str = ",".join(shopify_ids)
        url = self._url(f"/orders.json?ids={ids_str}&status=any")
        
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code != 200:
            _logger.error("Failed to fetch orders: %s", resp.text)
            return []
            
        return resp.json().get("orders", [])

    @staticmethod
    def validate_webhook(payload: bytes, signature: str, secret: str) -> bool:
        if not signature:
            return False
        digest = hmac.new(secret.encode(), payload, sha256).digest()
        computed = base64.b64encode(digest).decode()
        return hmac.compare_digest(computed, signature)

    def get_product_variant(self, variant_id: str) -> Optional[Dict[str, Any]]:
        """Fetch variant details to recover missing weight."""
        url = self._url(f"/variants/{variant_id}.json")
        try:
            resp = requests.get(url, headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                return resp.json().get("variant")
            else:
                _logger.warning("Failed to fetch variant %s: %s", variant_id, resp.status_code)
        except Exception as e:
            _logger.warning("Error fetching variant %s: %s", variant_id, e)
        return None
    def graphql_query(self, query: str) -> Dict[str, Any]:
        """Execute a GraphQL query."""
        url = self._url("/graphql.json")
        try:
            resp = requests.post(url, headers=self._headers(), json={"query": query}, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                _logger.error("GraphQL query failed: %s", resp.text)
        except Exception as e:
            _logger.exception("GraphQL request error: %s", e)
        return {}

    def get_weight_by_sku(self, sku: str) -> float:
        """Fetch weight in grams for a given SKU using GraphQL."""
        query = """
        {
          productVariants(first: 1, query: "sku:%s") {
            edges {
              node {
                weight
                weightUnit
              }
            }
          }
        }
        """ % sku
        data = self.graphql_query(query)
        try:
            edges = data.get("data", {}).get("productVariants", {}).get("edges", [])
            if edges:
                node = edges[0]["node"]
                weight = node["weight"]
                unit = node["weightUnit"]
                # Convert to grams
                if unit == "KILOGRAMS":
                    return weight * 1000.0
                elif unit == "GRAMS":
                    return weight
                elif unit == "POUNDS":
                    return weight * 453.592
                elif unit == "OUNCES":
                    return weight * 28.3495
        except Exception as e:
            _logger.error("Failed to parse weight from GraphQL response: %s", e)
        return 0.0

