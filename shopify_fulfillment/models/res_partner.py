from odoo import fields, models


class ResPartner(models.Model):
    """Extend res.partner with Shopify customer tracking."""

    _inherit = "res.partner"

    shopify_customer_id = fields.Char(
        string="Shopify Customer ID",
        index=True,
        help="The unique customer ID from Shopify, used to link orders to customers."
    )
