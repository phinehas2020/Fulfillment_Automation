from odoo import http
from odoo.http import request


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
        # TODO: validate HMAC, persist order + lines, trigger async processing
        payload = request.jsonrequest
        request.env["ir.logging"].sudo().create(
            {
                "name": "shopify_webhook_stub",
                "type": "server",
                "level": "INFO",
                "dbname": request.env.cr.dbname,
                "message": f"Received Shopify webhook placeholder: {payload}",
                "path": __name__,
                "line": "0",
                "func": "order_webhook",
            }
        )
        return {"status": "received"}


