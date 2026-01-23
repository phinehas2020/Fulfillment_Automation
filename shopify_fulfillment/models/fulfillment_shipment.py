from odoo import fields, models


class FulfillmentShipment(models.Model):
    """Shipment record for purchased labels and tracking."""

    _name = "fulfillment.shipment"
    _description = "Fulfillment Shipment"
    _order = "sequence, id"

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

    # Multi-box support fields
    group_id = fields.Many2one(
        "fulfillment.shipment.group",
        string="Shipment Group",
        ondelete="cascade",
        index=True,
    )
    box_id = fields.Many2one(
        "fulfillment.box",
        string="Box Used",
    )
    sequence = fields.Integer(
        default=1,
        string="Box #",
        help="Box number in multi-box shipment (1, 2, 3...)",
    )
    line_ids = fields.Many2many(
        "shopify.order.line",
        string="Items in Box",
        help="Order line items packed in this box",
    )
    total_weight = fields.Float(
        string="Total Weight (g)",
        help="Weight of items + box in grams",
    )



