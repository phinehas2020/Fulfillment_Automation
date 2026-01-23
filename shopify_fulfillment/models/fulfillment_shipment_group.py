from odoo import api, fields, models


class FulfillmentShipmentGroup(models.Model):
    """Groups multiple shipments for a single order (multi-box support)."""

    _name = "fulfillment.shipment.group"
    _description = "Shipment Group (Multi-Box)"
    _order = "create_date desc"

    order_id = fields.Many2one(
        "shopify.order",
        string="Order",
        required=True,
        ondelete="cascade",
        index=True,
    )
    shipment_ids = fields.One2many(
        "fulfillment.shipment",
        "group_id",
        string="Shipments",
    )
    shipment_count = fields.Integer(
        compute="_compute_totals",
        store=True,
        string="Box Count",
    )
    total_shipping_cost = fields.Float(
        compute="_compute_totals",
        store=True,
        string="Total Shipping Cost",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("partial", "Partial"),
            ("complete", "Complete"),
            ("error", "Error"),
        ],
        default="pending",
        string="Status",
    )

    @api.depends("shipment_ids", "shipment_ids.rate_amount")
    def _compute_totals(self):
        for group in self:
            group.shipment_count = len(group.shipment_ids)
            group.total_shipping_cost = sum(
                s.rate_amount or 0.0 for s in group.shipment_ids
            )

    def name_get(self):
        result = []
        for group in self:
            name = f"Ship Group #{group.id} ({group.shipment_count} boxes)"
            result.append((group.id, name))
        return result
