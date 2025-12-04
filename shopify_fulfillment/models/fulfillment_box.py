from odoo import models


class FulfillmentBox(models.Model):
    """Available box sizes."""

    _name = "fulfillment.box"
    _description = "Fulfillment Box"

    # TODO: add fields per specification (dimensions, weight, priority, etc.)


