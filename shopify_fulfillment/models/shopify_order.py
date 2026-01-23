import json
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

    # Multi-box support fields
    shipment_group_id = fields.Many2one(
        "fulfillment.shipment.group",
        string="Shipment Group",
    )
    shipment_ids = fields.One2many(
        "fulfillment.shipment",
        related="shipment_group_id.shipment_ids",
        string="Shipments",
    )
    is_multi_box = fields.Boolean(
        compute="_compute_multi_box_info",
        store=True,
        string="Multi-Box",
    )
    box_count = fields.Integer(
        compute="_compute_multi_box_info",
        store=True,
        string="Box Count",
    )
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
        readonly=True,
        help="Indicates if inventory has been deducted via a fulfillment task."
    )
    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sale Order",
        readonly=True,
        help="Linked Odoo Sale Order created upon fulfillment"
    )

    def _create_sale_order(self):
        """Create an Odoo sale.order from this Shopify order."""
        self.ensure_one()
        
        if self.sale_order_id:
            return self.sale_order_id  # Already exists
        
        # Find or create partner
        partner = self._create_or_update_partner()
        if not partner:
            _logger.warning("Could not create partner for order %s", self.order_name)
            return False
        
        # Parse raw payload for prices
        payload = {}
        if self.raw_payload:
            try:
                payload = json.loads(self.raw_payload)
            except Exception:
                pass
        
        line_items_data = {str(li.get("id")): li for li in payload.get("line_items", [])}
        
        # Prepare sale order lines
        order_lines = []
        for line in self.line_ids:
            if not line.requires_shipping:
                continue
                
            sku = (line.sku or "").strip()
            product = None
            
            # Find product by SKU (multiple strategies)
            if sku:
                # 1. Exact match on product variant
                product = self.env["product.product"].search([
                    ("default_code", "=", sku)
                ], limit=1)
                
                # 2. Case-insensitive match
                if not product:
                    product = self.env["product.product"].search([
                        ("default_code", "=ilike", sku)
                    ], limit=1)
                
                # 3. Template exact match
                if not product:
                    product = self.env["product.product"].search([
                        ("product_tmpl_id.default_code", "=", sku)
                    ], limit=1)
                
                # 4. Template case-insensitive match
                if not product:
                    product = self.env["product.product"].search([
                        ("product_tmpl_id.default_code", "=ilike", sku)
                    ], limit=1)
            
            if not product:
                _logger.warning("No product found for SKU '%s' in order %s - skipping line", sku, self.order_name)
                continue
            
            # Get price from Shopify payload
            price_unit = 0.0
            line_data = line_items_data.get(str(line.shopify_line_id), {})
            if line_data:
                try:
                    price_unit = float(line_data.get("price", 0))
                except (ValueError, TypeError):
                    pass
            
            order_lines.append((0, 0, {
                "product_id": product.id,
                "product_uom_qty": line.quantity,
                "price_unit": price_unit,
                "name": line.title or product.name,
            }))
        
        if not order_lines:
            _logger.warning("No valid lines for sale order creation: %s", self.order_name)
            return False
        
        # Add shipping line if we have shipment info
        if self.shipment_id and self.shipment_id.rate_amount:
            shipping_product = self.env["product.product"].search([
                ("default_code", "=", "SHIPPING")
            ], limit=1)
            
            if shipping_product:
                carrier_info = f"{self.shipment_id.carrier or ''} {self.shipment_id.service or ''}".strip()
                order_lines.append((0, 0, {
                    "product_id": shipping_product.id,
                    "product_uom_qty": 1,
                    "price_unit": self.shipment_id.rate_amount,
                    "name": f"Shipping - {carrier_info}" if carrier_info else "Shipping",
                }))
        
        # Create the sale order
        sale_vals = {
            "partner_id": partner.id,
            "origin": self.order_name or self.order_number,
            "client_order_ref": self.shopify_id,
            "order_line": order_lines,
        }
        
        sale_order = self.env["sale.order"].create(sale_vals)
        
        # Confirm the sale order (move from draft to sale)
        sale_order.action_confirm()
        
        self.sale_order_id = sale_order.id
        
        _logger.info("Created sale order %s for Shopify order %s", sale_order.name, self.order_name)
        return sale_order

    def action_create_fulfillment_task(self):
        """Manually create a fulfillment task for this order."""
        self.ensure_one()
        Task = self.env["project.task"]
        existing = Task.search([("shopify_order_id", "=", self.id), ("is_fulfillment_task", "=", True)], limit=1)
        if existing:
            raise exceptions.UserError("A fulfillment task already exists for this order.")

        ICP = self.env["ir.config_parameter"].sudo()
        default_user_id_raw = ICP.get_param("fulfillment.default_user_id")
        user_ids = [int(default_user_id_raw)] if default_user_id_raw else []

        description = "<ul>"
        for line in self.line_ids:
            if line.requires_shipping:
                description += f"<li>[{line.sku or 'NO SKU'}] <b>{line.title}</b> x{line.quantity}</li>"
        description += "</ul>"

        task = Task.create({
            "name": f"Pack Order {self.order_name or self.order_number}",
            "description": description,
            "user_ids": [(6, 0, user_ids)],
            "shopify_order_id": self.id,
            "is_fulfillment_task": True,
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": "project.task",
            "res_id": task.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_manual_inventory_deduction(self):
        """Force inventory deduction by finding or creating a task and running its deduction logic."""
        self.ensure_one()
        if self.inventory_deducted:
            raise exceptions.UserError("Inventory has already been marked as deducted for this order.")

        Task = self.env["project.task"]
        task = Task.search([("shopify_order_id", "=", self.id), ("is_fulfillment_task", "=", True)], limit=1)
        
        if not task:
            # Create a silent fulfillment task to perform the deduction
            ICP = self.env["ir.config_parameter"].sudo()
            default_user_id_raw = ICP.get_param("fulfillment.default_user_id")
            user_ids = [int(default_user_id_raw)] if default_user_id_raw else []
            
            task = Task.create({
                "name": f"Inventory Deduction (Manual) - {self.order_name}",
                "shopify_order_id": self.id,
                "is_fulfillment_task": True,
                "user_ids": [(6, 0, user_ids)],
                "state": "1_done", # Mark as done immediately
            })
            # The write override in project_task should trigger action_fulfillment_deduct_inventory
        else:
            # Trigger it manually on the existing task
            task.action_fulfillment_deduct_inventory()
            
        self._compute_inventory_status()
        return True

    @api.depends("fulfillment_task_ids.fulfillment_inventory_deducted")
    def _compute_inventory_status(self):
        for order in self:
            # Check if any linked fulfillment task has inventory deducted
            # We filter for is_fulfillment_task=True for accuracy
            tasks = order.fulfillment_task_ids.filtered(lambda t: t.is_fulfillment_task and t.fulfillment_inventory_deducted)
            order.inventory_deducted = bool(tasks)

    # Note: Removed the 'read' override to avoid Odoo 18 registry/compute loops.
    # Users should use the 'Sync' button to refresh status from Shopify manually.

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

    @api.depends("shipment_group_id", "shipment_group_id.shipment_ids")
    def _compute_multi_box_info(self):
        for order in self:
            if order.shipment_group_id:
                count = len(order.shipment_group_id.shipment_ids)
                order.box_count = count
                order.is_multi_box = count > 1
            else:
                order.box_count = 1 if order.shipment_id else 0
                order.is_multi_box = False

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

    def _create_or_update_partner(self):
        """Create or update res.partner from Shopify order data to build customer database."""
        self.ensure_one()
        
        # Extract customer ID from raw payload if available
        payload = {}
        if self.raw_payload:
            try:
                import json
                payload = json.loads(self.raw_payload)
            except Exception:
                pass
        
        customer_data = payload.get("customer", {})
        shopify_customer_id = str(customer_data.get("id", "")) if customer_data else ""
        
        # Also try to get email from customer data if not on order
        customer_email = self.email or customer_data.get("email", "")
        
        Partner = self.env["res.partner"].sudo()
        partner = None
        
        # Try to find existing partner by Shopify customer ID
        if shopify_customer_id:
            partner = Partner.search([("shopify_customer_id", "=", shopify_customer_id)], limit=1)
        
        # Fallback: find by email (case-insensitive)
        if not partner and customer_email:
            partner = Partner.search([("email", "=ilike", customer_email)], limit=1)
        
        # Build partner values
        vals = {
            "name": self.customer_name or "Unknown Customer",
            "email": customer_email,
            "phone": self.shipping_phone,
            "street": self.shipping_address_line1,
            "street2": self.shipping_address_line2,
            "city": self.shipping_city,
            "zip": self.shipping_zip,
            "customer_rank": 1,  # Mark as customer
        }
        
        # Set state if available
        if self.shipping_state:
            state = self.env["res.country.state"].search([
                ("code", "=", self.shipping_state),
                ("country_id.code", "=", self.shipping_country or "US")
            ], limit=1)
            if state:
                vals["state_id"] = state.id
        
        # Set country if available
        if self.shipping_country:
            country = self.env["res.country"].search([("code", "=", self.shipping_country)], limit=1)
            if country:
                vals["country_id"] = country.id
        
        # Add Shopify customer ID if we have it
        if shopify_customer_id:
            vals["shopify_customer_id"] = shopify_customer_id
        
        if partner:
            # Update existing (only update fields that have values)
            update_vals = {k: v for k, v in vals.items() if v}
            partner.write(update_vals)
            _logger.debug("Updated existing partner %s for order %s", partner.id, self.order_name)
        else:
            # Create new partner
            partner = Partner.create(vals)
            _logger.info("Created new partner %s (%s) for order %s", partner.id, partner.name, self.order_name)
        
        return partner

    @api.model
    def action_import_from_shopify(self):
        """
        Fetch all unfulfilled orders from Shopify and import any that don't exist yet.
        This is useful for catching orders missed during server downtime.
        """
        try:
            api = self._get_shopify_api()
        except Exception as e:
            raise exceptions.UserError(f"Shopify API not configured: {e}")
        
        _logger.info("Starting Shopify order sync...")
        
        # Fetch unfulfilled orders from Shopify
        shopify_orders = api.get_unfulfilled_orders()
        _logger.info("Found %d unfulfilled orders in Shopify", len(shopify_orders))
        
        imported_count = 0
        skipped_count = 0
        error_count = 0
        
        for order_data in shopify_orders:
            shopify_id = str(order_data.get("id"))
            
            # Skip POS orders
            if order_data.get("source_name") == "pos":
                _logger.debug("Skipping POS order %s", shopify_id)
                skipped_count += 1
                continue
            
            # Check if already exists
            existing = self.search([("shopify_id", "=", shopify_id)], limit=1)
            if existing:
                _logger.debug("Order %s already exists, skipping", shopify_id)
                skipped_count += 1
                continue
            
            # Prepare and create order
            try:
                order_vals = self._prepare_order_vals_from_shopify(order_data)
                order = self.create(order_vals)
                imported_count += 1
                _logger.info("Imported order %s (%s)", order.order_name, shopify_id)
                
                # Create/update customer in Odoo database
                try:
                    order._create_or_update_partner()
                except Exception as partner_err:
                    _logger.warning("Failed to create partner for order %s: %s", shopify_id, partner_err)
                
                # Check if auto-processing is enabled
                ICP = self.env["ir.config_parameter"].sudo()
                auto_process = ICP.get_param("fulfillment.auto_process", "False")
                if auto_process.lower() in ("true", "1", "yes"):
                    order.process_order()
                    _logger.info("Order %s auto-processed", order.id)
                    
            except Exception as e:
                _logger.exception("Failed to import order %s: %s", shopify_id, e)
                error_count += 1
        
        message = f"Shopify Sync Complete:\n• Imported: {imported_count}\n• Skipped (existing/POS): {skipped_count}\n• Errors: {error_count}"
        _logger.info(message)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Shopify Sync Complete',
                'message': f"Imported: {imported_count}, Skipped: {skipped_count}, Errors: {error_count}",
                'type': 'success' if error_count == 0 else 'warning',
                'sticky': False,
            }
        }

    def _prepare_order_vals_from_shopify(self, payload: dict):
        """Prepare order values from Shopify API response (same as webhook format)."""
        shipping = payload.get("shipping_address") or {}
        line_vals = []
        for line in payload.get("line_items", []):
            line_vals.append(
                (
                    0,
                    0,
                    {
                        "shopify_line_id": line.get("id"),
                        "shopify_product_id": line.get("product_id"),
                        "shopify_variant_id": line.get("variant_id"),
                        "sku": line.get("sku"),
                        "title": line.get("title"),
                        "variant_title": line.get("variant_title"),
                        "quantity": line.get("quantity") or 0,
                        "weight": line.get("grams") or 0.0,
                        "requires_shipping": line.get("requires_shipping", True),
                    },
                )
            )
        source = "amazon" if (payload.get("source_name") == "amazon" or "amazon" in (payload.get("tags") or "").lower()) else "shopify"
        
        shipping_lines = payload.get("shipping_lines") or []
        requested_method = shipping_lines[0].get("title") if shipping_lines else False
        
        created_at = False
        if payload.get("created_at"):
            try:
                from dateutil import parser
                dt = parser.parse(payload.get("created_at"))
                created_at = dt.replace(tzinfo=None)
            except Exception:
                pass
        
        return {
            "shopify_id": str(payload.get("id")),
            "order_number": payload.get("order_number"),
            "order_name": payload.get("name"),
            "email": payload.get("email"),
            "customer_name": f"{shipping.get('first_name', '')} {shipping.get('last_name', '')}".strip(),
            "shipping_address_line1": shipping.get("address1"),
            "shipping_address_line2": shipping.get("address2"),
            "shipping_city": shipping.get("city"),
            "shipping_state": shipping.get("province_code"),
            "shipping_zip": shipping.get("zip"),
            "shipping_country": shipping.get("country_code"),
            "shipping_phone": shipping.get("phone"),
            "created_at": created_at,
            "raw_payload": __import__('json').dumps(payload),
            "line_ids": line_vals,
            "source": source,
            "requested_shipping_method": requested_method,
        }

    def action_process(self):
        for order in self:
            order.process_order()

    def process_order(self):
        """End-to-end flow: box selection, rate shopping, label purchase, print job."""
        for order in self:
            try:
                # Ensure customer is in Odoo database before processing
                try:
                    order._create_or_update_partner()
                except Exception as partner_err:
                    _logger.warning("Failed to create partner for order %s during process: %s", order.id, partner_err)
                
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

        # Check if shipment group already exists (multi-box) or single shipment
        if self.shipment_group_id:
            group = self.shipment_group_id
            shipments_with_labels = group.shipment_ids.filtered(lambda s: s.label_zpl)

            if shipments_with_labels:
                # Re-print existing labels
                for shipment in shipments_with_labels:
                    self.env["print.job"].create({
                        "order_id": self.id,
                        "shipment_id": shipment.id,
                        "job_type": "label",
                        "zpl_data": shipment.label_zpl or "",
                        "printer_id": False,
                    })
                self.write({"state": "ready_to_ship"})
                return
            else:
                # Previous processing failed - delete empty/failed group and reprocess
                _logger.info("Order %s: Deleting failed shipment group %s to reprocess", self.id, group.id)
                group.shipment_ids.unlink()
                group.unlink()
                self.shipment_group_id = False

        if self.shipment_id:
            # Legacy single shipment - just create a print job
            self.env["print.job"].create({
                "order_id": self.id,
                "shipment_id": self.shipment_id.id,
                "job_type": "label",
                "zpl_data": self.shipment_id.label_zpl or "",
                "printer_id": False,
            })
            self.write({"state": "ready_to_ship"})
            return

        # Multi-box packing
        packing_result = self._pack_order_multi_box()

        if not packing_result.success:
            self.write({
                "state": "manual_required",
                "error_message": packing_result.error_message or "Packing failed"
            })
            return

        if not packing_result.packed_boxes:
            self.write({
                "state": "manual_required",
                "error_message": "No boxes assigned - check box configuration"
            })
            return

        # Check for oversized items requiring manual intervention
        if packing_result.has_oversized:
            oversized_count = sum(1 for pb in packing_result.packed_boxes if pb.is_oversized)
            self.write({
                "state": "manual_required",
                "error_message": f"Order contains {oversized_count} oversized item(s) exceeding box capacity"
            })
            return

        # Create shipment group
        group = self.env["fulfillment.shipment.group"].create({
            "order_id": self.id,
        })
        self.shipment_group_id = group.id

        # Import Shippo service
        from odoo.addons.shopify_fulfillment.services.shippo_service import ShippoService
        shippo = ShippoService.from_env(self.env)

        # Process each packed box
        shipments_created = []
        for sequence, packed_box in enumerate(packing_result.packed_boxes, start=1):
            try:
                shipment = self._process_single_box(
                    packed_box=packed_box,
                    group=group,
                    sequence=sequence,
                    shippo=shippo
                )
                if shipment:
                    shipments_created.append(shipment)
            except Exception as e:
                _logger.exception("Failed to process box %d for order %s", sequence, self.id)
                group.write({"state": "error"})
                self.write({
                    "state": "error",
                    "error_message": f"Box {sequence} failed: {str(e)}"
                })
                return

        # Update group state
        group.write({"state": "complete"})

        # Backward compatibility: set shipment_id and box_id to first shipment
        if shipments_created:
            self.shipment_id = shipments_created[0].id
            self.box_id = shipments_created[0].box_id.id

        _logger.info("Order %s: Created %d shipments (multi-box)", self.id, len(shipments_created))
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

    def _pack_order_multi_box(self):
        """Run multi-box packing algorithm.

        Returns a PackingResult with packed_boxes list.
        """
        from odoo.addons.shopify_fulfillment.services.multi_box_packer import (
            MultiBoxPacker,
            PackingResult,
        )

        boxes = self.env["fulfillment.box"].search([("active", "=", True)])
        if not boxes:
            return PackingResult(success=False, error_message="No active boxes configured")

        boxes_data = [
            {
                "id": b.id,
                "name": b.name,
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

        packer = MultiBoxPacker.from_order(self, boxes_data)
        result = packer.pack()

        _logger.info(
            "Order %s: Packing result - %d boxes, success=%s",
            self.id,
            result.box_count,
            result.success,
        )
        return result

    def _process_single_box(self, packed_box, group, sequence: int, shippo) -> Optional[models.Model]:
        """Process a single box: rate shop, purchase label, create shipment, print job.

        Args:
            packed_box: PackedBox instance from packer
            group: fulfillment.shipment.group record
            sequence: Box number (1, 2, 3...)
            shippo: ShippoService instance

        Returns:
            fulfillment.shipment record or None
        """
        box_record = self.env["fulfillment.box"].browse(packed_box.box_spec.box_id)
        line_ids = packed_box.line_ids

        _logger.info(
            "Order %s: Processing box %d (%s) - %.0fg, %d items",
            self.id,
            sequence,
            box_record.name,
            packed_box.total_weight_with_box,
            len(packed_box.items),
        )

        shipment_vals = None

        if shippo:
            # Get rates for this specific box with its weight
            rates = shippo.get_rates_for_box(
                order=self,
                box=box_record,
                total_weight_grams=packed_box.total_weight_with_box,
                sender_company=self.env.company,
            )

            # Filter out excluded shipping services
            original_count = len(rates)
            rates = [
                r
                for r in rates
                if "ground saver" not in (r.get("servicelevel", {}).get("name") or "").lower()
            ]
            if original_count != len(rates):
                _logger.info(
                    "Order %s Box %d: Filtered out %d excluded services",
                    self.id,
                    sequence,
                    original_count - len(rates),
                )

            if not rates:
                raise exceptions.UserError(
                    f"Box {sequence}: Shippo returned no rates (Check address/credentials)"
                )

            # Select rate (cheapest or requested method)
            selected_rate = self._select_shipping_rate(rates)
            shipment_vals = shippo.purchase_label(selected_rate)
        else:
            # Fallback to Mock API (for testing)
            api_client = self._get_shopify_api()
            rates = api_client.get_shipping_rates(self)
            if not rates:
                raise exceptions.UserError(f"Box {sequence}: Mock API returned no rates")
            cheapest = sorted(rates, key=lambda r: r.get("amount", 0))[0]
            shipment_vals = api_client.purchase_label(self, cheapest.get("id"))

        if not shipment_vals:
            raise exceptions.UserError(f"Box {sequence}: Label purchase failed (unknown error)")

        if shipment_vals.get("error"):
            raise exceptions.UserError(f"Box {sequence}: {shipment_vals['error']}")

        # Create shipment record
        shipment = self.env["fulfillment.shipment"].create({
            "order_id": self.id,
            "group_id": group.id,
            "box_id": box_record.id,
            "sequence": sequence,
            "line_ids": [(6, 0, line_ids)],
            "total_weight": packed_box.total_weight_with_box,
            "carrier": shipment_vals.get("carrier"),
            "service": shipment_vals.get("service"),
            "tracking_number": shipment_vals.get("tracking_number"),
            "tracking_url": shipment_vals.get("tracking_url"),
            "label_url": shipment_vals.get("label_url"),
            "label_zpl": shipment_vals.get("label_zpl"),
            "rate_amount": shipment_vals.get("rate_amount"),
            "rate_currency": shipment_vals.get("rate_currency"),
            "purchased_at": fields.Datetime.now(),
        })

        # Create print job
        self.env["print.job"].create({
            "order_id": self.id,
            "shipment_id": shipment.id,
            "job_type": "label",
            "zpl_data": shipment.label_zpl or "",
            "printer_id": False,
        })

        _logger.info(
            "Order %s Box %d: Shipment created - %s %s",
            self.id,
            sequence,
            shipment.tracking_number,
            shipment.carrier,
        )
        return shipment

    def _select_shipping_rate(self, rates: list) -> dict:
        """Select the best shipping rate from available options.

        Prefers requested_shipping_method if set, otherwise cheapest.
        """
        if not rates:
            return {}

        # Sort by amount (cheapest first)
        cheapest = sorted(rates, key=lambda r: float(r.get("amount", 999999)))[0]
        selected_rate = cheapest

        if self.requested_shipping_method:
            req_norm = self.requested_shipping_method.strip().lower()
            for r in rates:
                s_name = r.get("servicelevel", {}).get("name", "").strip().lower()
                if s_name == req_norm:
                    selected_rate = r
                    _logger.info(
                        "Order %s: Found matching rate for '%s': %s - $%s",
                        self.id,
                        self.requested_shipping_method,
                        r.get("servicelevel", {}).get("name"),
                        r.get("amount"),
                    )
                    break
            else:
                _logger.warning(
                    "Order %s: Requested shipping '%s' not found. Using cheapest.",
                    self.id,
                    self.requested_shipping_method,
                )

        return selected_rate
