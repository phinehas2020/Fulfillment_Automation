from odoo import fields, models


class ShopifyOrderLine(models.Model):
    """Line items for Shopify orders."""

    _name = "shopify.order.line"
    _description = "Shopify Order Line"

    order_id = fields.Many2one("shopify.order", required=True, ondelete="cascade")
    shopify_line_id = fields.Char()
    shopify_product_id = fields.Char()
    shopify_variant_id = fields.Char()
    sku = fields.Char()
    title = fields.Char()
    variant_title = fields.Char()
    quantity = fields.Integer(default=1)
    weight = fields.Float(help="Weight in grams")
    requires_shipping = fields.Boolean(default=True)



