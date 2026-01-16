import logging
from typing import Optional

from odoo import api, exceptions, fields, models

_logger = logging.getLogger(__name__)


class ShopifyOrder(models.Model):
    """Shopify order stub model."""

    _name = "shopify.order"
    _description = "Shopify Order"
    _rec_name = "order_name"
    _sql_constraints = [
        ("shopify_id_unique", "unique(shopify_id)", "Shopify order already exists."),
    ]

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
    active = fields.Boolean(default=True)
    created_at = fields.Datetime()
    raw_payload = fields.Text()
    requested_shipping_method = fields.Char(string="Requested Shipping Method")
    shopify_risk_level = fields.Selection(
        [("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low")], 
        string="Shopify Risk Level",
        help="Risk level fetched from Shopify (High, Medium, Low)"
    )
    fulfillment_task_ids = fields.One2many("project.task", "shopify_order_id", string="Fulfillment Tasks")
    inventory_deducted = fields.Boolean(
        string="Inventory Deducted", 
        compute="_compute_inventory_status", 
        store=True,
        help="Indicates if inventory has been deducted via a fulfillment task."
    )

    @api.depends("fulfillment_task_ids.fulfillment_inventory_deducted")
    def _compute_inventory_status(self):
        for order in self:
            order.inventory_deducted = any(t.fulfillment_inventory_deducted for t in order.fulfillment_task_ids)

    def read(self, fields=None, load="_classic_read"):
        """Override read to sync status from Shopify on load."""
        if not self.env.context.get("shopify_sync_done") and (fields is None or "state" in fields):
            try:
                # Avoid syncing if we are purely in a computation loop or low-level access
                # But here we want to catch the view load.
                self.with_context(shopify_sync_done=True)._sync_shopify_status()
            except Exception as e:
                _logger.warning("Failed to sync Shopify status on read: %s", e)
        return super().read(fields=fields, load=load)

    def _sync_shopify_status(self):
        """Fetch latest status from Shopify and update local state."""
        # Identify records that need syncing
        # We only sync records that have a shopify_id and are not already archived (though self should be active usually)
        # We process in batches
        records_to_sync = self.filtered(lambda r: r.shopify_id and r.active)
        if not records_to_sync:
            return

        api = self._get_shopify_api()
        
        # Batch by 50
        batch_size = 50
        record_list = list(records_to_sync)
        for i in range(0, len(record_list), batch_size):
            batch = record_list[i : i + batch_size]
            shopify_ids = [r.shopify_id for r in batch]
            
            try:
                shopify_orders = api.get_orders(shopify_ids)
                self._update_local_orders(batch, shopify_orders)
            except Exception as e:
                _logger.error("Error syncing batch: %s", e)

    def _update_local_orders(self, batch_records, shopify_data):
        """Update records based on Shopify data."""
        data_map = {str(order["id"]): order for order in shopify_data}
        
        for record in batch_records:
            data = data_map.get(record.shopify_id)
            if not data:
                continue
            
            ff_status = data.get("fulfillment_status")
            financial_status = data.get("financial_status")
            
            # Logic: If fulfilled, remove from Odoo (archive)
            # fulfillment_status can be: null, fulfilled, partial, restocked
            if ff_status == "fulfilled":
                record.active = False
            elif ff_status == "partial":
                 # Keep it, maybe update state?
                 pass
            elif ff_status is None:
                # Unfulfilled
                pass
            
            # Additional Sync: If cancelled, maybe archive too?
            if data.get("cancelled_at"):
                record.active = False

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

    def _is_high_risk(self):
        """Check for high risk factors using Shopify Risk Level."""
        if not self.shopify_risk_level:
             # Fetch it if missing
             try:
                 api = self._get_shopify_api()
                 risk = api.get_risk_level(self.shopify_id)
                 # Write immediately to save for future
                 self.sudo().write({"shopify_risk_level": risk})
                 # If HIGH, return True
                 if risk == 'HIGH':
                     return True
             except Exception as e:
                 _logger.error("Failed to fetch risk level: %s", e)
                 
        return self.shopify_risk_level == 'HIGH'

    def _send_risk_notification(self):
        """Send email to risk reviewer."""
        ICP = self.env['ir.config_parameter'].sudo()
        reviewer_id_str = ICP.get_param('fulfillment.risk_reviewer_id')
        if not reviewer_id_str:
            _logger.info("No risk reviewer configured. Skipping notification.")
            return
            
        try:
            reviewer_id = int(reviewer_id_str)
            reviewer = self.env['res.users'].browse(reviewer_id)
        except (ValueError, TypeError):
            _logger.error("Invalid risk reviewer ID configured: %s", reviewer_id_str)
            return

        if not reviewer or not reviewer.email:
             _logger.warning("Risk reviewer has no email configured.")
             return

        subject = f"URGENT: High Risk Order Flagged - {self.name_get()[0][1]}"
        body = f"""
        <div style="font-family: Arial, sans-serif;">
            <h2>High Risk Order Detected</h2>
            <p><strong>Order:</strong> {self.order_name}</p>
            <p><strong>Shopify Risk Level:</strong> <span style="color: red; font-weight: bold;">{self.shopify_risk_level}</span></p>
            <p><strong>Customer:</strong> {self.customer_name}</p>
            <p><strong>Address:</strong><br/>
               {self.shipping_address_line1}<br/>
               {self.shipping_address_line2 or ''}<br/>
               {self.shipping_city}, {self.shipping_state} {self.shipping_zip}
            </p>
            <p>This order has been flagged by Shopify as High Risk. Please verify it in Odoo before manual processing.</p>
            <p><a href="/web#id={self.id}&model=shopify.order&view_type=form">View Order</a></p>
        </div>
        """
        
        mail_values = {
            'subject': subject,
            'body_html': body,
            'email_to': reviewer.email,
            'email_from': self.env.user.email_formatted or 'noreply@yourcompany.com',
        }
        try:
            self.env['mail.mail'].create(mail_values).send()
            _logger.info("Risk notification sent to %s", reviewer.email)
        except Exception as e:
            _logger.error("Failed to send risk notification: %s", e)

    def action_sync_status(self):
        """Manual action to sync status from Shopify."""
        # Use existing logic but ensure we force it
        try:
            # The existing private method handles batching self, but if called from action, self contains selected records
            self._sync_shopify_status()
        except Exception as e:
            raise exceptions.UserError(f"Sync failed: {e}")

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

        # Step 0: Risk Check
        if self._is_high_risk():
            _logger.warning("Order %s flagged as high risk. Stopping processing.", self.id)
            self.write({
                "state": "manual_required", 
                "error_message": "Flagged as High Risk/Spam. Notification sent for verification."
            })
            self._send_risk_notification()
            return

        if not self.line_ids:
            raise exceptions.UserError("Order has no line items")

        # Auto-recover missing weights from Shopify
        if any(l.requires_shipping and not l.weight for l in self.line_ids):
            api_client = self._get_shopify_api()
            fixed_count = 0
            for line in self.line_ids:
                if line.requires_shipping and not line.weight:
                    _logger.info("Validation: Line has 0 weight. Fetching details for Line %s...", line.id)

                    variant = None
                    # Strategy 1: Use Variant ID if exists
                    if line.shopify_variant_id:
                        variant = api_client.get_product_variant(line.shopify_variant_id)
                        if variant:
                            weight_g = variant.get("grams") or 0.0
                            if weight_g:
                                line.write({"weight": weight_g})
                                fixed_count += 1
                                continue  # Success, move to next line

                    # Strategy 2: If failed or no ID, lookup by SKU
                    if line.sku:
                        _logger.info("Validation: Fetching weight by SKU %s match...", line.sku)
                        weight_g = api_client.get_weight_by_sku(line.sku)
                        if weight_g:
                            _logger.info("Found weight by SKU: %s", weight_g)
                            line.write({"weight": weight_g})
                            fixed_count += 1

            if fixed_count > 0:
                self._compute_totals()  # Force recompute

        # Basic validation: weights present (check again after recovery attempt)
        if any(l.requires_shipping and not l.weight for l in self.line_ids):
            self.write({"state": "manual_required", "error_message": "Missing weight on one or more items (Fetch failed)"})
            return

        self.write({"state": "processing"})

        # Check if shipment already exists to avoid re-purchasing
        shipment = self.shipment_id
        if shipment:
             # Just create a print job and skip rate shopping
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
             return

        # Box selection
        box = self._select_box()
        if not box:
            msg = f"No box fits order. Total Weight: {self.total_weight}g"
            _logger.warning("Order %s: %s", self.id, msg)
            self.write({"state": "manual_required", "error_message": msg})
            return
        self.box_id = box.id

        # Rate Shopping
        # Import internally to avoid top-level loading issues
        from odoo.addons.shopify_fulfillment.services.shippo_service import ShippoService
        shippo = ShippoService.from_env(self.env)
        
        shipment_vals = None
        
        if shippo:
            rates = shippo.get_rates(self, box, self.env.company)
            if not rates:
                msg = "Shippo returned no rates (Check address/credentials)"
                _logger.warning("Order %s: %s", self.id, msg)
                self.write({"state": "manual_required", "error_message": msg})
                return
            # Sort by amount
            cheapest = sorted(rates, key=lambda r: float(r.get("amount", 999999)))[0]
            
            selected_rate = cheapest
            if self.requested_shipping_method:
                _logger.info("Order %s: User requested shipping '%s'", self.id, self.requested_shipping_method)
                # Try to find a match
                # User said "ill set up all they same carriors" -> implying exact name match
                req_norm = self.requested_shipping_method.strip().lower()
                
                # First pass: Look for exact match in servicelevel name
                found = None
                for r in rates:
                    s_name = r.get("servicelevel", {}).get("name", "").strip().lower()
                    if s_name == req_norm:
                        found = r
                        break
                
                if found:
                    selected_rate = found
                    _logger.info("Order %s: Found matching rate for '%s': %s - $%s", 
                                 self.id, self.requested_shipping_method, 
                                 found.get("servicelevel", {}).get("name"), found.get("amount"))
                else:
                    _logger.warning("Order %s: Requested shipping '%s' not found in rates. Using cheapest.", 
                                    self.id, self.requested_shipping_method)

            shipment_vals = shippo.purchase_label(selected_rate)
        else:
            # Fallback to Mock
            api_client = self._get_shopify_api()
            rates = api_client.get_shipping_rates(self)
            if not rates:
                msg = "Mock API returned no rates"
                _logger.warning("Order %s: %s", self.id, msg)
                self.write({"state": "manual_required", "error_message": msg})
                return
            cheapest = sorted(rates, key=lambda r: r.get("amount", 0))[0]
            shipment_vals = api_client.purchase_label(self, cheapest.get("id"))

        if not shipment_vals:
             # Generic failure
            raise exceptions.UserError("Label purchase failed (unknown error)")
            
        if shipment_vals.get("error"):
            # Specific failure from provider
            self.write({"state": "error", "error_message": shipment_vals["error"]})
            return

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

        # Basic heuristic: assume density ~ 9 g per cubic inch (approx for flour/grains)
        estimated_volume = self.total_weight / 9.0 if self.total_weight else 0

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
        
        from odoo.addons.shopify_fulfillment.services import box_selector
        selected_id = box_selector.select_box(data, self.total_weight, estimated_volume)
        
        if not selected_id:
            msg = f"No box fits. Wt: {self.total_weight}g, Est.Vol: {int(estimated_volume)}in³"
            _logger.warning("Order %s: %s", self.id, msg)
            self.write({"state": "manual_required", "error_message": msg})
            return None
            
        return boxes.browse(selected_id)

    def _estimate_volume(self) -> float:
        # Deprecated: logic moved inside _select_box for now.
        if self.total_weight:
            return max(self.total_weight / 9.0, 1.0)
        return 1.0
