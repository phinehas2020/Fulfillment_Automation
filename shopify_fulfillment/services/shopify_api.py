"""Shopify Admin API helper."""

import base64
import hmac
import logging
from hashlib import sha256
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

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

    def create_fulfillment(
        self,
        order,
        tracking_info: Dict[str, Any],
        line_items: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        Create a fulfillment in Shopify using the Fulfillment Orders API.

        Args:
            order: shopify.order record
            tracking_info: dict with tracking_number/tracking_url/carrier
            line_items: optional list of {"shopify_line_id": str, "quantity": int}
                limiting the fulfillment to specific items (multi-box orders
                create one fulfillment per box). When omitted, all open items
                are fulfilled.
        """
        fulfillment_orders = self._get_fulfillable_orders(order.shopify_id)
        if not fulfillment_orders:
            raise exceptions.UserError("No open fulfillment order found in Shopify.")

        if line_items:
            wanted = {}
            for item in line_items:
                key = str(item.get("shopify_line_id") or "")
                quantity = int(item.get("quantity") or 0)
                if key and quantity > 0:
                    wanted[key] = wanted.get(key, 0) + quantity

            by_fulfillment_order = []
            for fo in fulfillment_orders:
                fo_lines = []
                for fo_line in fo.get("line_items", []):
                    key = str(fo_line.get("line_item_id") or "")
                    remaining = wanted.get(key, 0)
                    if remaining <= 0:
                        continue
                    fulfillable = int(fo_line.get("fulfillable_quantity") or 0)
                    quantity = min(remaining, fulfillable)
                    if quantity <= 0:
                        continue
                    fo_lines.append({"id": fo_line.get("id"), "quantity": quantity})
                    wanted[key] = remaining - quantity
                if fo_lines:
                    by_fulfillment_order.append(
                        {
                            "fulfillment_order_id": fo.get("id"),
                            "fulfillment_order_line_items": fo_lines,
                        }
                    )

            if not by_fulfillment_order:
                raise exceptions.UserError(
                    "No fulfillable Shopify line items matched this shipment."
                )
        else:
            # Fulfill all open items on the first fulfillable order.
            by_fulfillment_order = [
                {"fulfillment_order_id": fulfillment_orders[0].get("id")}
            ]

        payload = {
            "fulfillment": {
                "line_items_by_fulfillment_order": by_fulfillment_order,
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

    def _get_fulfillable_orders(self, shopify_order_id: str) -> List[Dict[str, Any]]:
        """Fetch fulfillment orders that still have items to fulfill.

        Includes "in_progress" as well as "open": after the first box of a
        multi-box order is fulfilled, Shopify moves the fulfillment order to
        in_progress even though other items remain fulfillable.
        """
        url = self._url(f"/orders/{shopify_order_id}/fulfillment_orders.json")
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code != 200:
            _logger.error("Failed to fetch fulfillment orders: %s", resp.text)
            return []

        data = resp.json()
        return [
            fo
            for fo in data.get("fulfillment_orders", [])
            if fo.get("status") in ("open", "in_progress")
        ]

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

    def get_unfulfilled_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch all unfulfilled orders from Shopify.
        Handles pagination to get all orders.
        """
        all_orders = []
        # fulfillment_status=unfulfilled gets orders that haven't been fulfilled yet
        # Also exclude cancelled orders
        url = self._url(f"/orders.json?fulfillment_status=unfulfilled&status=open&limit={limit}")
        
        while url:
            try:
                resp = requests.get(url, headers=self._headers(), timeout=30)
                if resp.status_code != 200:
                    _logger.error("Failed to fetch unfulfilled orders: %s", resp.text)
                    break
                    
                data = resp.json()
                orders = data.get("orders", [])
                all_orders.extend(orders)
                
                _logger.info("Fetched %d unfulfilled orders (total: %d)", len(orders), len(all_orders))
                
                # Check for pagination - Shopify uses Link header
                link_header = resp.headers.get("Link", "")
                url = None
                if 'rel="next"' in link_header:
                    # Parse the next URL from Link header
                    for part in link_header.split(","):
                        if 'rel="next"' in part:
                            url = part.split(";")[0].strip().strip("<>")
                            break
            except Exception as e:
                _logger.exception("Error fetching unfulfilled orders: %s", e)
                break
                
        return all_orders

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

    @staticmethod
    def _truthy_metafield_value(value) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _normalized_metafield_key(value: str) -> str:
        return "".join(ch for ch in (value or "").lower() if ch.isalnum())

    def product_has_true_metafield(self, product_id: str, metafield_key: str) -> bool:
        """Return True when a product metafield exists and has a truthy value."""
        if not product_id:
            return False

        target_key = self._normalized_metafield_key(metafield_key)
        url = self._url(f"/products/{product_id}/metafields.json?limit=250")
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code >= 400:
            raise exceptions.UserError(
                f"Product metafield lookup failed for product {product_id}: {resp.text}"
            )

        for metafield in resp.json().get("metafields", []):
            key = metafield.get("key")
            if self._normalized_metafield_key(key) != target_key:
                continue
            return self._truthy_metafield_value(metafield.get("value"))

        return False

    @staticmethod
    def _coerce_metafield_number(value):
        """Best-effort numeric coercion for restock-related metafields."""
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value).strip()
            if not text:
                return None
            if "." in text:
                return float(text)
            return int(text)
        except (TypeError, ValueError):
            return None

    def _fetch_metafields(self, owner_kind: str, owner_id: str) -> List[Dict[str, Any]]:
        """Fetch metafields for either a product or variant. Returns [] on missing/error."""
        if not owner_id:
            return []
        if owner_kind not in ("products", "variants"):
            raise ValueError(f"Unsupported metafield owner kind: {owner_kind}")
        url = self._url(f"/{owner_kind}/{owner_id}/metafields.json?limit=250")
        try:
            resp = requests.get(url, headers=self._headers(), timeout=15)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.warning("Metafield fetch failed for %s/%s: %s", owner_kind, owner_id, exc)
            return []
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            _logger.warning(
                "Metafield fetch returned %s for %s/%s: %s",
                resp.status_code, owner_kind, owner_id, resp.text,
            )
            return []
        return resp.json().get("metafields", []) or []

    def get_variant_restock_metafields(
        self,
        variant_id: Optional[str],
        product_id: Optional[str],
        namespace: str = "custom",
    ) -> Dict[str, Optional[float]]:
        """Return {restock_level, desired_inventory_level} for a variant.

        Variant-level metafields take precedence over product-level. Returns Nones when
        a metafield is missing so the caller can skip silently.
        """
        target_keys = {
            "restock_level": "restocklevel",
            "desired_inventory_level": "desiredinventorylevel",
        }
        result: Dict[str, Optional[float]] = {key: None for key in target_keys}

        def _harvest(metafields):
            for metafield in metafields or []:
                if metafield.get("namespace") and metafield["namespace"] != namespace:
                    continue
                norm_key = self._normalized_metafield_key(metafield.get("key"))
                for out_key, want in target_keys.items():
                    if norm_key == want and result[out_key] is None:
                        result[out_key] = self._coerce_metafield_number(metafield.get("value"))

        if variant_id:
            _harvest(self._fetch_metafields("variants", str(variant_id)))
        if product_id and any(value is None for value in result.values()):
            _harvest(self._fetch_metafields("products", str(product_id)))
        return result

    def get_variant_inventory_item_id(self, variant_id: str) -> str:
        """Fetch the inventory item ID for a Shopify variant."""
        variant = self.get_product_variant(variant_id)
        if not variant:
            raise exceptions.UserError(f"Shopify variant {variant_id} was not found.")

        inventory_item_id = variant.get("inventory_item_id")
        if not inventory_item_id:
            raise exceptions.UserError(
                f"Shopify variant {variant_id} does not have an inventory item ID."
            )
        return str(inventory_item_id)

    def get_inventory_level(self, inventory_item_id: str, location_id: str) -> Optional[Dict[str, Any]]:
        """Fetch an inventory level by inventory item and Shopify location."""
        params = urlencode(
            {
                "inventory_item_ids": inventory_item_id,
                "location_ids": location_id,
            }
        )
        url = self._url(f"/inventory_levels.json?{params}")
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code >= 400:
            raise exceptions.UserError(
                f"Inventory level lookup failed for item {inventory_item_id} "
                f"at location {location_id}: {resp.text}"
            )

        levels = resp.json().get("inventory_levels", [])
        return levels[0] if levels else None

    def get_available_inventory_quantity(self, inventory_item_id: str, location_id: str) -> float:
        """Return Shopify's available quantity for an item at a location."""
        level = self.get_inventory_level(inventory_item_id, location_id)
        if not level:
            raise exceptions.UserError(
                f"No Shopify inventory level found for item {inventory_item_id} "
                f"at location {location_id}."
            )

        available = level.get("available")
        if available is None:
            raise exceptions.UserError(
                f"Shopify inventory level for item {inventory_item_id} "
                f"at location {location_id} has no available quantity."
            )

        try:
            return float(available)
        except (TypeError, ValueError) as exc:
            raise exceptions.UserError(
                f"Invalid Shopify available quantity for item {inventory_item_id}: {available}"
            ) from exc

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

    @staticmethod
    def _strongest_risk_level(levels: List[str]) -> Optional[str]:
        rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        normalized = [str(level or "").upper() for level in levels]
        normalized = [level for level in normalized if level in rank]
        if not normalized:
            return None
        return max(normalized, key=lambda level: rank[level])

    @classmethod
    def _risk_level_from_summary(cls, risk_summary: Dict[str, Any]) -> Optional[str]:
        if not risk_summary:
            return None

        recommendation = str(risk_summary.get("recommendation") or "").upper()
        recommendation_levels = {
            "CANCEL": "HIGH",
            "INVESTIGATE": "MEDIUM",
            "ACCEPT": "LOW",
            "NONE": "LOW",
        }
        levels = []
        if recommendation in recommendation_levels:
            levels.append(recommendation_levels[recommendation])

        assessments = risk_summary.get("assessments") or []
        for assessment in assessments:
            assessment_level = str(assessment.get("riskLevel") or "").upper()
            if assessment_level == "PENDING":
                levels.append("MEDIUM")
            else:
                levels.append(assessment_level)
        return cls._strongest_risk_level(levels)

    def _get_risk_level_from_rest(self, shopify_order_id: str) -> str:
        numeric_id = str(shopify_order_id).split("/")[-1]
        url = self._url(f"/orders/{numeric_id}/risks.json")
        resp = requests.get(url, headers=self._headers(), timeout=15)
        if resp.status_code >= 400:
            raise exceptions.UserError(
                f"Shopify REST order risk lookup failed for {numeric_id}: {resp.status_code} {resp.text}"
            )

        payload = resp.json()
        risks = payload.get("risks") or []
        levels = []
        for risk in risks:
            recommendation = str(risk.get("recommendation") or "").lower()
            if recommendation == "cancel":
                levels.append("HIGH")
            elif recommendation == "investigate":
                levels.append("MEDIUM")
            elif recommendation == "accept":
                levels.append("LOW")

            if str(risk.get("score") or "").isdigit():
                score = int(risk["score"])
                if score >= 2:
                    levels.append("HIGH")
                elif score == 1:
                    levels.append("MEDIUM")
                else:
                    levels.append("LOW")

        return self._strongest_risk_level(levels) or "LOW"

    def get_risk_level(self, shopify_order_id: str) -> str:
        """Fetch Shopify risk level (HIGH, MEDIUM, LOW) without failing open."""
        gid = shopify_order_id
        if not str(gid).startswith("gid://"):
            gid = f"gid://shopify/Order/{shopify_order_id}"

        query = """
        {
          order(id: "%s") {
            risk {
              recommendation
              assessments {
                riskLevel
              }
            }
          }
        }
        """ % gid

        data = self.graphql_query(query)
        errors = data.get("errors") or []
        if errors:
            _logger.warning("Shopify GraphQL risk lookup returned errors for %s: %s", shopify_order_id, errors)
        else:
            order_data = data.get("data", {}).get("order") or {}
            risk_level = self._risk_level_from_summary(order_data.get("risk") or {})
            if risk_level:
                return risk_level

        return self._get_risk_level_from_rest(shopify_order_id)
