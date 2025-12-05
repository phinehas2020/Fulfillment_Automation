import logging
from typing import Optional

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


class ShopifyOrder(models.Model):
    """Shopify order stub model."""

    _name = "shopify.order"
    _description = "Shopify Order"

    shopify_id = fields.Char(required=True, index=True)
    order_number = fields.Char(string="Order Number")
    order_name = fields.Char(string="Order Name")
    email = fields.Char()
    customer_name = fields.Char()
    shipping_address_line1 = fields.Char()
    shipping_address_line2 = fields.Char()
    shipping_city = fields.Char()
    shipping_state = fields.Char()
    shipping_zip = fields.Char()
    shipping_country = fields.Char()
    shipping_phone = fields.Char()
    total_weight = fields.Float(compute="_compute_totals", store=True, help="Total weight in grams")
    total_items = fields.Integer(compute="_compute_totals", store=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("ready_to_ship", "Ready to Ship"),
            ("shipped", "Shipped"),
            ("error", "Error"),
            ("manual_required", "Manual Review"),
        ],
        default="pending",
    )
    error_message = fields.Text()
    source = fields.Selection([("shopify", "Shopify"), ("amazon", "Amazon")], default="shopify")
    line_ids = fields.One2many("shopify.order.line", "order_id", string="Order Lines")
    shipment_id = fields.Many2one("fulfillment.shipment", string="Shipment")
    print_job_ids = fields.One2many("print.job", "order_id", string="Print Jobs")
    box_id = fields.Many2one("fulfillment.box", string="Selected Box")
    created_at = fields.Datetime()
    raw_payload = fields.Text()

    def _get_shopify_api(self):
        from ..services.shopify_api import ShopifyAPI

        return ShopifyAPI.from_env(self.env)

    @api.depends("line_ids.weight", "line_ids.quantity")
    def _compute_totals(self):
        for order in self:
            total_weight = sum((l.weight or 0.0) * (l.quantity or 0) for l in order.line_ids)
            total_items = sum(l.quantity or 0 for l in order.line_ids)
            order.total_weight = total_weight
            order.total_items = total_items

    def action_process(self):
        for order in self:
            order.process_order()

    def process_order(self):
        """End-to-end flow: box selection, rate shopping, label purchase, print job."""
        for order in self:
            try:
                order._process_order_inner()
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Order processing failed for %s", order.id)
                order.write({"state": "error", "error_message": str(exc)})

    def _process_order_inner(self):
        self.ensure_one()
        if not self.line_ids:
            raise exceptions.UserError("Order has no line items")

        # Basic validation: weights present
        if any(l.requires_shipping and not l.weight for l in self.line_ids):
            self.write({"state": "manual_required", "error_message": "Missing weight on one or more items"})
            return

        self.write({"state": "processing"})

        # Box selection
        box = self._select_box()
        if not box:
            self.write({"state": "manual_required", "error_message": "No box fits order"})
            return
        self.box_id = box.id

        # Rate Shopping
        from ..services.shippo_service import ShippoService
        shippo = ShippoService.from_env(self.env)
        
        shipment_vals = None
        
        if shippo:
            rates = shippo.get_rates(self, box, self.env.company)
            if not rates:
                self.write({"state": "manual_required", "error_message": "Shippo returned no rates"})
                return
            # Sort by amount
            cheapest = sorted(rates, key=lambda r: float(r.get("amount", 999999)))[0]
            shipment_vals = shippo.purchase_label(cheapest)
        else:
            # Fallback to Mock
            api_client = self._get_shopify_api()
            rates = api_client.get_shipping_rates(self)
            if not rates:
                self.write({"state": "manual_required", "error_message": "No shipping rates returned"})
                return
            cheapest = sorted(rates, key=lambda r: r.get("amount", 0))[0]
            shipment_vals = api_client.purchase_label(self, cheapest.get("id"))

        if not shipment_vals:
            raise exceptions.UserError("Label purchase failed or returned empty data")

        # Push Fulfillment to Shopify
        api_client = self._get_shopify_api()
        try:
            # We only push if we have a valid tracking number
            if shipment_vals.get("tracking_number"):
                ff_resp = api_client.create_fulfillment(self, shipment_vals)
                if ff_resp and ff_resp.get("fulfillment"):
                    shipment_vals["shopify_fulfillment_id"] = ff_resp["fulfillment"]["id"]
        except Exception as e:
            _logger.error("Failed to update Shopify fulfillment: %s", e)
            # We continue because we still want to save the label and print it

        shipment = self.env["fulfillment.shipment"].create(
            {
                "order_id": self.id,
                "carrier": shipment_vals.get("carrier"),
                "service": shipment_vals.get("service"),
                "tracking_number": shipment_vals.get("tracking_number"),
                "tracking_url": shipment_vals.get("tracking_url"),
                "label_url": shipment_vals.get("label_url"),
                "label_zpl": shipment_vals.get("label_zpl"),
                "rate_amount": shipment_vals.get("rate_amount"),
                "rate_currency": shipment_vals.get("rate_currency"),
                "shopify_fulfillment_id": shipment_vals.get("shopify_fulfillment_id"),
                "purchased_at": fields.Datetime.now(),
            }
        )
        self.shipment_id = shipment.id

        # Create print job
        self.env["print.job"].create(
            {
                "order_id": self.id,
                "shipment_id": shipment.id,
                "job_type": "label",
                "zpl_data": shipment.label_zpl or "",
                "printer_id": False,
            }
        )
        self.write({"state": "ready_to_ship"})

    def _select_box(self) -> Optional[models.Model]:
        boxes = self.env["fulfillment.box"].search([("active", "=", True)])
        if not boxes:
            return None

        data = [
            {
                "id": b.id,
                "length": b.length,
                "width": b.width,
                "height": b.height,
                "max_weight": b.max_weight,
                "box_weight": b.box_weight,
                "volume": b.volume,
                "priority": b.priority,
            }
            for b in boxes
        ]
        estimated_volume = self._estimate_volume()
        from ..services import box_selector

        selected_id = box_selector.select_box(data, self.total_weight, estimated_volume)
        if not selected_id:
            return None
        return boxes.browse(selected_id)

    def _estimate_volume(self) -> float:
        # Basic heuristic: assume density ~ 5 g per cubic inch if no better data.
        if self.total_weight:
            return max(self.total_weight / 5.0, 1.0)
        return 1.0



