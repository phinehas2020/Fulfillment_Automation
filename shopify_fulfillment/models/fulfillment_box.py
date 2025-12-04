from odoo import api, fields, models


class FulfillmentBox(models.Model):
    """Available box sizes."""

    _name = "fulfillment.box"
    _description = "Fulfillment Box"

    name = fields.Char(required=True)
    length = fields.Float(help="Interior length (inches)")
    width = fields.Float(help="Interior width (inches)")
    height = fields.Float(help="Interior height (inches)")
    max_weight = fields.Float(help="Max capacity (ounces)")
    box_weight = fields.Float(help="Empty box weight (ounces)")
    volume = fields.Float(compute="_compute_volume", store=True, help="Volume (cubic inches)")
    active = fields.Boolean(default=True)
    priority = fields.Integer(default=100, help="Lower = preferred when volumes are close")

    @api.depends("length", "width", "height")
    def _compute_volume(self):
        for rec in self:
            if rec.length and rec.width and rec.height:
                rec.volume = rec.length * rec.width * rec.height
            else:
                rec.volume = 0.0



