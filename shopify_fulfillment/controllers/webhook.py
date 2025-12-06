import base64
import hmac
import json
import logging
from hashlib import sha256

from odoo import http
from odoo.http import request

from ..services.shopify_api import ShopifyAPI

_logger = logging.getLogger(__name__)


class ShopifyWebhookController(http.Controller):
    """Receives Shopify order webhooks. HMAC validation and processing TBD."""

    @http.route(
        "/shopify/webhook/order",
        type="json",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def order_webhook(self, **kwargs):
        _ = kwargs  # unused
        raw_body = request.httprequest.data
        signature = request.httprequest.headers.get("X-Shopify-Hmac-Sha256")

        secret = request.env["ir.config_parameter"].sudo().get_param("shopify.webhook_secret")
        if not secret:
            return http.Response("Webhook secret not configured", status=500)

        if not self._validate_hmac(raw_body, signature, secret):
            _logger.warning("Invalid Shopify webhook signature")
            return http.Response("Invalid signature", status=401)

        payload = json.loads(raw_body.decode("utf-8"))
        order_model = request.env["shopify.order"].sudo()

        existing = order_model.search([("shopify_id", "=", str(payload.get("id")))], limit=1)
        if existing:
            return {"status": "duplicate", "order_id": existing.id}

        order_vals = self._prepare_order_vals(payload)
        order = order_model.create(order_vals)
        # AUTO-PROCESS DISABLED - Uncomment to re-enable automatic fulfillment
        # order.process_order()
        return {"status": "ok", "order_id": order.id}

    @staticmethod
    def _validate_hmac(payload: bytes, signature: str, secret: str) -> bool:
        if not signature:
            return False
        digest = hmac.new(secret.encode(), payload, sha256).digest()
        computed = base64.b64encode(digest).decode()
        return hmac.compare_digest(computed, signature)

    def _prepare_order_vals(self, payload: dict):
        shipping = payload.get("shipping_address") or {}
        line_vals = []
        for line in payload.get("line_items", []):
            line_vals.append(
                (
                    0,
                    0,
                    {
                        "shopify_line_id": line.get("id"),
                        "shopify_product_id": line.get("product_id"),
                        "shopify_variant_id": line.get("variant_id"),
                        "sku": line.get("sku"),
                        "title": line.get("title"),
                        "variant_title": line.get("variant_title"),
                        "quantity": line.get("quantity") or 0,
                        "weight": line.get("grams") or 0.0,
                        "requires_shipping": line.get("requires_shipping", True),
                    },
                )
            )
        source = "amazon" if (payload.get("source_name") == "amazon" or "amazon" in (payload.get("tags") or "").lower()) else "shopify"
        return {
            "shopify_id": str(payload.get("id")),
            "order_number": payload.get("order_number"),
            "order_name": payload.get("name"),
            "email": payload.get("email"),
            "customer_name": f"{shipping.get('first_name', '')} {shipping.get('last_name', '')}".strip(),
            "shipping_address_line1": shipping.get("address1"),
            "shipping_address_line2": shipping.get("address2"),
            "shipping_city": shipping.get("city"),
            "shipping_state": shipping.get("province_code"),
            "shipping_zip": shipping.get("zip"),
            "shipping_country": shipping.get("country_code"),
            "shipping_phone": shipping.get("phone"),
            "created_at": self._parse_date(payload.get("created_at")),
            "raw_payload": json.dumps(payload),
            "line_ids": line_vals,
            "source": source,
        }

    @staticmethod
    def _parse_date(date_str: str):
        if not date_str:
            return False
        try:
            from dateutil import parser
            dt = parser.parse(date_str)
            return dt.replace(tzinfo=None)  # Odoo expects naive UTC
        except Exception:
            return False


