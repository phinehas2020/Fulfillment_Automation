from odoo import exceptions, fields, models


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
    purchase_provider = fields.Selection(
        [
            ("shippo", "Shippo"),
            ("amazon_shipping", "Amazon Buy Shipping"),
            ("mock", "Mock/Test"),
        ],
        default="shippo",
        required=True,
        index=True,
        string="Label Provider",
    )
    provider_shipment_id = fields.Char(string="Provider Shipment ID", index=True)
    provider_rate_id = fields.Char(string="Provider Rate ID")
    provider_service_id = fields.Char(string="Provider Service ID")
    provider_purchase_reference = fields.Char(string="Purchase Reference", index=True)
    provider_cancel_reference = fields.Char(string="Cancellation Reference")
    provider_payload_json = fields.Text(string="Provider Purchase Payload")
    provider_response_json = fields.Text(string="Provider Response")
    purchase_state = fields.Selection(
        [
            ("pending", "Purchase Pending"),
            ("submitting", "Purchase Submitting"),
            ("purchased", "Purchased"),
            ("uncertain", "Purchase Uncertain"),
            ("error", "Purchase Error"),
        ],
        default="purchased",
        required=True,
        index=True,
    )
    label_format = fields.Char(string="Label Format")
    promised_delivery_at = fields.Datetime(string="Promised Delivery")
    line_quantities = fields.Text(
        help="JSON mapping of shopify.order.line ID to the number of units "
        "packed in this box, so multi-box fulfillments can attach tracking "
        "to the right items in Shopify.",
    )
    shippo_transaction_id = fields.Char(string="Shippo Transaction ID", index=True)
    shippo_refund_id = fields.Char(string="Shippo Refund ID", readonly=True)
    refund_status = fields.Selection(
        [
            ("not_requested", "Not Requested"),
            ("queued", "Queued"),
            ("pending", "Pending"),
            ("uncertain", "Cancellation Uncertain"),
            ("success", "Success"),
            ("error", "Error"),
        ],
        default="not_requested",
        string="Cancellation / Refund Status",
        readonly=True,
    )
    refund_requested_at = fields.Datetime(readonly=True)
    refund_error_message = fields.Text(readonly=True)
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

    def action_reconcile_amazon_purchase(self):
        """Recover documents for a known Amazon shipment without repurchasing."""
        from odoo.addons.shopify_fulfillment.services.amazon_shipping_service import (
            AmazonShippingService,
        )

        amazon = AmazonShippingService.from_env(self.env, require_enabled=False)
        if not amazon:
            raise exceptions.UserError("Amazon Shipping credentials are unavailable.")

        for shipment in self:
            if shipment.purchase_provider != "amazon_shipping":
                raise exceptions.UserError(
                    "Only Amazon Buy Shipping purchases can use this reconciliation action."
                )
            if shipment.purchase_state not in ("submitting", "uncertain"):
                continue
            if not shipment.provider_shipment_id:
                raise exceptions.UserError(
                    "Amazon did not return a shipment ID. Check Amazon Buy Shipping "
                    "before deciding whether this intent can be retried."
                )
            result = amazon.get_shipment_documents(
                shipment.provider_shipment_id,
                shipment.provider_purchase_reference,
            )
            if result.get("error"):
                raise exceptions.UserError(result["error"])
            shipment.write({
                "purchase_state": "purchased",
                "tracking_number": result.get("tracking_number"),
                "label_zpl": result.get("label_zpl"),
                "label_format": result.get("label_format") or "ZPL",
                "provider_response_json": result.get("provider_response_json"),
                "purchased_at": shipment.purchased_at or fields.Datetime.now(),
            })
            shipment.order_id._finalize_deferred_purchase_group()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Amazon Purchase Reconciled",
                "message": "Recovered the existing Amazon label without repurchasing.",
                "type": "success",
                "sticky": False,
            },
        }
