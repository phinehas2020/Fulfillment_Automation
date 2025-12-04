from odoo import models


class ShopifyOrderLine(models.Model):
    """Line items for Shopify orders."""

    _name = "shopify.order.line"
    _description = "Shopify Order Line"

    # TODO: add fields per specification (order_id, sku, quantity, weight, etc.)


