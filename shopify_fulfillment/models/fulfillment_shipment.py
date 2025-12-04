from odoo import fields, models


class FulfillmentShipment(models.Model):
    """Shipment record for purchased labels and tracking."""

    _name = "fulfillment.shipment"
    _description = "Fulfillment Shipment"

    order_id = fields.Many2one("shopify.order", ondelete="cascade")
    carrier = fields.Char()
    service = fields.Char()
    tracking_number = fields.Char()
    tracking_url = fields.Char()
    label_url = fields.Char()
    label_zpl = fields.Text()
    rate_amount = fields.Float()
    rate_currency = fields.Char()
    shopify_fulfillment_id = fields.Char()
    purchased_at = fields.Datetime()



