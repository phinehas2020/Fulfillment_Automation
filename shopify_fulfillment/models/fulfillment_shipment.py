from odoo import models


class FulfillmentShipment(models.Model):
    """Shipment record for purchased labels and tracking."""

    _name = "fulfillment.shipment"
    _description = "Fulfillment Shipment"

    # TODO: add fields per specification (tracking_number, carrier, label data, etc.)


