import json
import logging
import re
import unicodedata
from datetime import timezone
from html import escape
from typing import Optional

from odoo import api, exceptions, fields, models
from ..services.address_utils import normalize_address_lines
from ..services.pickup_utils import (
    fulfillment_orders_confirm_pickup,
    payload_has_ambiguous_physical_fulfillment,
    payload_is_pickup,
)

_logger = logging.getLogger(__name__)


class ShippingPolicyHold(exceptions.UserError):
    """A deliberate pre-purchase hold requiring human review."""


class ShopifyOrder(models.Model):
    """Shopify order stub model."""

    _name = "shopify.order"
    _description = "Shopify Order"
    _rec_name = "order_name"
    _order = "created_at desc, id desc"
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
            ("pickup_pending", "Pickup Task Created"),
            ("pickup_completed", "Pickup Completed"),
            ("inventory_synced", "Inventory Synced"),
            ("error", "Error"),
            ("manual_required", "Manual Review"),
        ],
        default="pending",
    )
    error_message = fields.Text()
    auto_process_queued = fields.Boolean(
        default=False,
        help="Set when the order arrives with auto-processing enabled; the "
        "processing cron picks these up so webhooks can return immediately.",
    )
    source = fields.Selection(
        [("shopify", "Shopify"), ("amazon", "Amazon"), ("pos", "POS")],
        default="shopify",
    )
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
    order_currency = fields.Char(string="Order Currency", default="USD")
    shipping_amount_paid = fields.Float(string="Customer Shipping Paid")
    amazon_order_id = fields.Char(string="Amazon Order ID", index=True, readonly=True)
    amazon_earliest_ship_at = fields.Datetime(string="Amazon Earliest Ship", readonly=True)
    amazon_latest_ship_at = fields.Datetime(string="Amazon Latest Ship", readonly=True)
    amazon_earliest_delivery_at = fields.Datetime(
        string="Amazon Earliest Delivery", readonly=True
    )
    amazon_latest_delivery_at = fields.Datetime(
        string="Amazon Latest Delivery", readonly=True
    )
    fulfillment_type = fields.Selection(
        [("shipping", "Shipping"), ("pickup", "Pickup")],
        string="Fulfillment Type",
        default="shipping",
        required=True,
        index=True,
    )
    pickup_notification_state = fields.Selection(
        [
            ("not_applicable", "Not Applicable"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("error", "Error"),
        ],
        string="Pickup Teams Notification",
        default="not_applicable",
        required=True,
        copy=False,
    )
    pickup_notification_sent_at = fields.Datetime(
        string="Pickup Notification Sent At",
        readonly=True,
        copy=False,
    )
    pickup_notification_error = fields.Text(
        string="Pickup Notification Error",
        readonly=True,
        copy=False,
    )
    pickup_notification_attempts = fields.Integer(
        string="Pickup Notification Attempts",
        readonly=True,
        copy=False,
    )
    shopify_location_id = fields.Char(string="Shopify Location ID", index=True)
    pos_inventory_synced_at = fields.Datetime(string="POS Inventory Synced At", readonly=True)
    pos_inventory_sync_summary = fields.Text(string="POS Inventory Sync Summary", readonly=True)
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

    @staticmethod
    def _join_customer_name(first_name, last_name):
        parts = [str(part).strip() for part in (first_name, last_name) if part and str(part).strip()]
        return " ".join(parts).strip()

    @staticmethod
    def _extract_customer_name_from_payload(payload: dict):
        payload = payload or {}
        customer = payload.get("customer") or {}
        address_sources = [
            payload.get("shipping_address") or {},
            payload.get("billing_address") or {},
            customer.get("default_address") or {},
            customer,
        ]

        for source in address_sources:
            full_name = (source.get("name") or "").strip()
            if full_name:
                return full_name

            joined = ShopifyOrder._join_customer_name(
                source.get("first_name"),
                source.get("last_name"),
            )
            if joined:
                return joined

        email = (payload.get("email") or customer.get("email") or "").strip()
        return email or False

    @staticmethod
    def _source_from_payload(payload: dict):
        payload = payload or {}
        source_name = (payload.get("source_name") or "").lower()
        tags = (payload.get("tags") or "").lower()
        if source_name == "pos":
            return "pos"
        if source_name.startswith("amazon") or "amazon" in tags:
            return "amazon"
        return "shopify"

    @staticmethod
    def _fulfillment_type_from_payload(payload: dict):
        return "pickup" if payload_is_pickup(payload or {}) else "shipping"

    @api.model
    def _fulfillment_classification_vals(self, payload: dict):
        fulfillment_type = self._fulfillment_type_from_payload(payload)
        vals = {"fulfillment_type": fulfillment_type}
        if fulfillment_type == "pickup":
            vals["pickup_notification_state"] = "queued"
        elif payload_has_ambiguous_physical_fulfillment(payload or {}):
            vals.update({
                "state": "manual_required",
                "error_message": (
                    "Physical Shopify order has no shipping address and no explicit "
                    "pickup method. Manual review is required."
                ),
            })
        return vals

    @staticmethod
    def _shopify_location_id_from_payload(payload: dict):
        location_id = (payload or {}).get("location_id")
        return str(location_id) if location_id else False

    def _get_customer_display_name(self):
        self.ensure_one()

        if (self.customer_name or "").strip():
            return self.customer_name.strip()

        payload = {}
        if self.raw_payload:
            try:
                payload = json.loads(self.raw_payload)
            except Exception:
                _logger.debug("Order %s has invalid raw payload JSON for customer-name fallback", self.id)

        payload_name = self._extract_customer_name_from_payload(payload)
        if payload_name:
            return payload_name

        if (self.email or "").strip():
            return self.email.strip()

        return "Unknown Customer"

    def _get_fulfillment_task_title(self):
        self.ensure_one()
        return self._get_customer_display_name()

    def _get_fulfillment_task_description(self):
        self.ensure_one()

        order_reference = self.order_name or self.order_number or self.shopify_id or ""
        parts = []
        if self.fulfillment_type == "pickup":
            parts.append(
                "<p><strong>Pickup order:</strong> Prepare this order, mark it ready "
                "in Shopify, then finish this Odoo task after preparation.</p>"
            )
        if order_reference:
            parts.append(f"<p><strong>Order:</strong> {escape(str(order_reference))}</p>")

        shop_domain = (
            self.env["ir.config_parameter"].sudo().get_param("shopify.shop_domain", "")
            or ""
        ).strip()
        if self.fulfillment_type == "pickup" and shop_domain and self.shopify_id:
            order_url = f"https://{shop_domain}/admin/orders/{self.shopify_id}"
            parts.append(
                f'<p><a href="{escape(order_url)}">Open pickup order in Shopify</a></p>'
            )

        parts.append("<ul>")
        for line in self.line_ids:
            if not line.requires_shipping:
                continue

            sku = escape(line.sku or "NO SKU")
            title = escape(line.title or "Untitled Item")
            parts.append(f"<li>[{sku}] <b>{title}</b> x{line.quantity}</li>")
        parts.append("</ul>")

        return "".join(parts)

    def _get_default_fulfillment_user_ids(self):
        self.ensure_one()

        default_user_id_raw = self.env["ir.config_parameter"].sudo().get_param("fulfillment.default_user_id")
        if not default_user_id_raw:
            return []

        try:
            return [int(default_user_id_raw)]
        except (TypeError, ValueError):
            _logger.warning("Invalid fulfillment.default_user_id value: %s", default_user_id_raw)
            return []

    def _should_refresh_fulfillment_task_name(self, current_name, target_name):
        self.ensure_one()

        current_name = (current_name or "").strip()
        target_name = (target_name or "").strip()
        if not target_name or current_name == target_name:
            return False
        if not current_name:
            return True

        order_reference = (self.order_name or self.order_number or "").strip()
        legacy_names = {
            order_reference,
            f"Pack Order {order_reference}".strip(),
            f"Inventory Deduction (Manual) - {order_reference}".strip(),
        }
        return current_name in legacy_names

    def ensure_fulfillment_task(self, state=None):
        self.ensure_one()

        Task = self.env["project.task"]
        existing = Task.search(
            [("shopify_order_id", "=", self.id), ("is_fulfillment_task", "=", True)],
            limit=1,
        )
        target_name = self._get_fulfillment_task_title()
        target_description = self._get_fulfillment_task_description()

        if existing:
            updates = {}
            if self._should_refresh_fulfillment_task_name(existing.name, target_name):
                updates["name"] = target_name
            if not existing.description and target_description:
                updates["description"] = target_description
            if updates:
                existing.write(updates)
            return existing

        vals = {
            "name": target_name,
            "description": target_description,
            "user_ids": [(6, 0, self._get_default_fulfillment_user_ids())],
            "shopify_order_id": self.id,
            "is_fulfillment_task": True,
        }
        if state:
            vals["state"] = state

        return Task.create(vals)

    def action_create_fulfillment_task(self):
        """Manually create a fulfillment task for this order."""
        self.ensure_one()
        task = self.ensure_fulfillment_task()
        return {
            "type": "ir.actions.act_window",
            "res_model": "project.task",
            "res_id": task.id,
            "view_mode": "form",
            "target": "current",
        }

    def _confirm_pickup_delivery_method(self):
        """Confirm explicit pickup classification when Shopify exposes a method."""
        self.ensure_one()
        if self.fulfillment_type != "pickup":
            return False
        try:
            method_types = self._get_shopify_api().get_fulfillment_delivery_method_types(
                self.shopify_id
            )
        except Exception as exc:  # pylint: disable=broad-except
            _logger.warning(
                "Order %s: Shopify pickup confirmation unavailable: %s",
                self.order_name,
                exc,
            )
            return True

        confirmed = fulfillment_orders_confirm_pickup(method_types)
        if confirmed is False:
            self.write({
                "state": "manual_required",
                "error_message": (
                    "Shopify order payload says pickup, but its fulfillment-order "
                    "delivery method is not pickup. Manual review is required."
                ),
                "auto_process_queued": False,
            })
            return False
        return True

    def _send_pickup_notification(self):
        self.ensure_one()
        if self.fulfillment_type != "pickup":
            return False
        if self.pickup_notification_state == "sent":
            return True

        task = self.ensure_fulfillment_task()
        from ..services.alert_service import AlertService

        success, error = AlertService.from_env(self.env).notify_pickup_assignee(
            order=self,
            task=task,
        )
        vals = {
            "pickup_notification_attempts": self.pickup_notification_attempts + 1,
        }
        if success:
            vals.update({
                "pickup_notification_state": "sent",
                "pickup_notification_sent_at": fields.Datetime.now(),
                "pickup_notification_error": False,
            })
            task.message_post(body="Pickup notification accepted by the Teams workflow.")
        else:
            vals.update({
                "pickup_notification_state": "error",
                "pickup_notification_error": error or "Pickup Teams notification failed.",
            })
            task.message_post(body=f"Pickup Teams notification failed: {escape(error or '')}")
        self.write(vals)
        return success

    def _process_pickup_order(self):
        self.ensure_one()
        if not self._confirm_pickup_delivery_method():
            return False

        self.ensure_fulfillment_task()
        self.write({
            "state": "pickup_pending",
            "error_message": False,
            "auto_process_queued": False,
        })
        self._send_pickup_notification()
        return True

    def action_retry_pickup_notification(self):
        for order in self:
            if order.fulfillment_type != "pickup":
                continue
            order.write({
                "pickup_notification_state": "queued",
                "pickup_notification_error": False,
            })
            order._send_pickup_notification()
        return True

    def action_manual_inventory_deduction(self):
        """Force inventory deduction by finding or creating a task and running its deduction logic."""
        self.ensure_one()
        if self.inventory_deducted:
            raise exceptions.UserError("Inventory has already been marked as deducted for this order.")

        Task = self.env["project.task"]
        task = Task.search([("shopify_order_id", "=", self.id), ("is_fulfillment_task", "=", True)], limit=1)
        
        if not task:
            task = self.ensure_fulfillment_task(state="1_done")
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
        records_to_sync = self.filtered(lambda r: r.shopify_id and r.active and r.source != "pos")
        if not records_to_sync:
            return

        try:
            api = self._get_shopify_api()
        except Exception as exc:  # pylint: disable=broad-except
            return self._mark_pos_inventory_sync_manual_required(str(exc))
        
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

    def _payload_dict(self):
        self.ensure_one()
        if not self.raw_payload:
            return {}
        try:
            return json.loads(self.raw_payload)
        except Exception:
            _logger.warning("Order %s has invalid raw payload JSON", self.id)
            return {}

    def _get_shopify_pos_location_id(self):
        self.ensure_one()
        payload_location_id = self._shopify_location_id_from_payload(self._payload_dict())
        return (self.shopify_location_id or payload_location_id or "").strip()

    def _find_odoo_product_by_sku(self, sku: str):
        sku = (sku or "").strip()
        if not sku:
            return self.env["product.product"]

        Product = self.env["product.product"].sudo()
        product = Product.search([("default_code", "=", sku)], limit=1)
        if not product:
            product = Product.search([("default_code", "=ilike", sku)], limit=1)
        if not product:
            product = Product.search([("product_tmpl_id.default_code", "=", sku)], limit=1)
        if not product:
            product = Product.search([("product_tmpl_id.default_code", "=ilike", sku)], limit=1)
        return product

    def _get_configured_stock_location(self):
        ICP = self.env["ir.config_parameter"].sudo()
        location_id_raw = ICP.get_param("fulfillment.stock_location_id")
        if not location_id_raw:
            raise exceptions.UserError("Please configure a Source Stock Location in Shopify Settings first.")

        try:
            location_id = int(location_id_raw)
        except (TypeError, ValueError) as exc:
            raise exceptions.UserError(
                f"Configured Source Stock Location is invalid: {location_id_raw}"
            ) from exc

        location = self.env["stock.location"].sudo().browse(location_id)
        if not location.exists():
            raise exceptions.UserError("Configured Source Stock Location was not found.")
        return location

    def _get_configured_pos_stock_location(self):
        ICP = self.env["ir.config_parameter"].sudo()
        location_id_raw = ICP.get_param("fulfillment.pos_stock_location_id")
        if location_id_raw:
            try:
                location_id = int(location_id_raw)
            except (TypeError, ValueError) as exc:
                raise exceptions.UserError(
                    f"Configured POS Retail Stock Location is invalid: {location_id_raw}"
                ) from exc

            location = self.env["stock.location"].sudo().browse(location_id)
            if location.exists():
                return location
            raise exceptions.UserError("Configured POS Retail Stock Location was not found.")

        Warehouse = self.env["stock.warehouse"].sudo()
        retail_warehouse = Warehouse.search([("name", "=ilike", "Retail")], limit=1)
        if not retail_warehouse:
            retail_warehouse = Warehouse.search([("name", "ilike", "Retail")], limit=1)
        if retail_warehouse and retail_warehouse.lot_stock_id:
            return retail_warehouse.lot_stock_id

        Location = self.env["stock.location"].sudo()
        retail_location = Location.search(
            [
                ("usage", "=", "internal"),
                "|",
                ("complete_name", "ilike", "HGR/Main Room"),
                ("complete_name", "ilike", "Retail"),
            ],
            limit=1,
        )
        if not retail_location:
            retail_location = Location.search(
                [("usage", "=", "internal"), ("name", "=ilike", "Main Room")],
                limit=1,
            )
        if retail_location:
            return retail_location

        raise exceptions.UserError(
            "Please configure a POS Retail Stock Location in Shopify Settings first."
        )

    def _get_exact_available_quantity(self, product, location):
        Quant = self.env["stock.quant"].sudo()
        try:
            return Quant._get_available_quantity(product, location, strict=True)
        except TypeError:
            return product.sudo().with_context(location=location.id).qty_available

    def _set_exact_available_quantity(self, product, location, target_qty):
        Quant = self.env["stock.quant"].sudo()
        current_qty = self._get_exact_available_quantity(product, location)
        delta = target_qty - current_qty
        if delta:
            Quant._update_available_quantity(product, location, delta)
        return current_qty, target_qty

    @staticmethod
    def _format_pos_line_for_error(line):
        sku = (line.sku or "NO SKU").strip()
        title = (line.title or "Untitled item").strip()
        variant_title = (line.variant_title or "").strip()
        if variant_title and variant_title.lower() != "default title":
            title = f"{title} / {variant_title}"
        return f"{sku} - {title}"

    def _mark_pos_inventory_sync_manual_required(self, message: str):
        self.ensure_one()
        self.write(
            {
                "state": "manual_required",
                "error_message": message,
                "pos_inventory_sync_summary": message,
                "pos_inventory_synced_at": False,
            }
        )
        _logger.warning("POS inventory sync blocked for order %s: %s", self.order_name, message)
        return False

    def action_retry_pos_inventory_sync(self):
        """Manual retry for POS orders after fixing missing products or config."""
        synced_count = 0
        for order in self:
            if order.source != "pos":
                continue
            if order._sync_pos_inventory_from_shopify():
                synced_count += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "POS Inventory Sync",
                "message": f"Synced {synced_count} POS order(s).",
                "type": "success" if synced_count else "warning",
                "sticky": False,
            },
        }

    def _sync_pos_inventory_from_shopify(self):
        self.ensure_one()
        if self.source != "pos":
            raise exceptions.UserError("POS inventory sync can only run on POS orders.")

        shopify_location_id = self._get_shopify_pos_location_id()
        if not shopify_location_id:
            return self._mark_pos_inventory_sync_manual_required(
                "POS inventory sync blocked: Shopify order has no location_id."
            )

        self.shopify_location_id = shopify_location_id

        try:
            stock_location = self._get_configured_pos_stock_location()
        except exceptions.UserError as exc:
            return self._mark_pos_inventory_sync_manual_required(str(exc))

        api = self._get_shopify_api()
        preflight_errors = []
        skipped_lines = []
        sync_rows = []

        for line in self.line_ids:
            try:
                if api.product_has_true_metafield(line.shopify_product_id, "baked_goods"):
                    skipped_lines.append(
                        f"{self._format_pos_line_for_error(line)}: baked goods product"
                    )
                    continue
            except Exception as exc:  # pylint: disable=broad-except
                preflight_errors.append(f"{self._format_pos_line_for_error(line)}: {exc}")
                continue

            if not line.shopify_variant_id:
                if not line.sku and not line.shopify_product_id:
                    skipped_lines.append(self._format_pos_line_for_error(line))
                    continue
                preflight_errors.append(
                    f"{self._format_pos_line_for_error(line)}: missing Shopify variant ID"
                )
                continue

            sku = (line.sku or "").strip()
            if not sku:
                preflight_errors.append(
                    f"{self._format_pos_line_for_error(line)}: missing SKU for Odoo product match"
                )
                continue

            product = self._find_odoo_product_by_sku(sku)
            if not product:
                preflight_errors.append(
                    f"{self._format_pos_line_for_error(line)}: no matching Odoo product"
                )
                continue

            try:
                inventory_item_id = api.get_variant_inventory_item_id(line.shopify_variant_id)
                available_qty = api.get_available_inventory_quantity(
                    inventory_item_id,
                    shopify_location_id,
                )
            except Exception as exc:  # pylint: disable=broad-except
                preflight_errors.append(f"{self._format_pos_line_for_error(line)}: {exc}")
                continue

            try:
                restock_metafields = api.get_variant_restock_metafields(
                    line.shopify_variant_id,
                    line.shopify_product_id,
                )
            except Exception:  # pylint: disable=broad-except
                _logger.exception(
                    "Failed to fetch restock metafields for variant %s; skipping restock check",
                    line.shopify_variant_id,
                )
                restock_metafields = {"restock_level": None, "desired_inventory_level": None}

            sync_rows.append(
                {
                    "line": line,
                    "product": product,
                    "inventory_item_id": inventory_item_id,
                    "available_qty": available_qty,
                    "restock_metafields": restock_metafields,
                }
            )

        if preflight_errors:
            self._run_pos_restock_detection_from_rows(
                sync_rows,
                shopify_location_id,
                "POS inventory sync was blocked by another line",
            )
            message = "POS inventory sync blocked. No Odoo stock was changed:\n"
            message += "\n".join(f"- {error}" for error in preflight_errors)
            if sync_rows:
                message += (
                    "\n\nRestock detection still ran for valid lines on this order."
                )
            if skipped_lines:
                message += "\n\nSkipped ignored lines:\n"
                message += "\n".join(f"- {line}" for line in skipped_lines)
            return self._mark_pos_inventory_sync_manual_required(message)

        if not sync_rows:
            summary = "POS inventory sync completed: no syncable Shopify product lines found."
            if skipped_lines:
                summary += "\nSkipped ignored lines:\n"
                summary += "\n".join(f"- {line}" for line in skipped_lines)
            self.write(
                {
                    "state": "inventory_synced",
                    "error_message": False,
                    "pos_inventory_sync_summary": summary,
                    "pos_inventory_synced_at": fields.Datetime.now(),
                }
            )
            return True

        updates_by_product = {}
        for row in sync_rows:
            product = row["product"]
            existing = updates_by_product.get(product.id)
            if existing and existing["available_qty"] != row["available_qty"]:
                preflight_errors.append(
                    "%s: multiple Shopify variants map to %s with conflicting quantities (%s vs %s)"
                    % (
                        self._format_pos_line_for_error(row["line"]),
                        product.display_name,
                        existing["available_qty"],
                        row["available_qty"],
                    )
                )
                continue

            updates_by_product[product.id] = {
                "product": product,
                "available_qty": row["available_qty"],
                "lines": (existing["lines"] if existing else []) + [row["line"]],
            }

        if preflight_errors:
            self._run_pos_restock_detection_from_rows(
                sync_rows,
                shopify_location_id,
                "POS inventory sync was blocked by conflicting variant quantities",
            )
            message = "POS inventory sync blocked. No Odoo stock was changed:\n"
            message += "\n".join(f"- {error}" for error in preflight_errors)
            if sync_rows:
                message += (
                    "\n\nRestock detection still ran for valid lines on this order."
                )
            return self._mark_pos_inventory_sync_manual_required(message)

        summary_lines = []
        for update in updates_by_product.values():
            product = update["product"]
            target_qty = update["available_qty"]
            old_qty, new_qty = self._set_exact_available_quantity(product, stock_location, target_qty)
            line_refs = ", ".join(
                str(line.sku or line.title or line.shopify_variant_id or "line").strip()
                for line in update["lines"]
            )
            summary_lines.append(
                f"{product.display_name}: {old_qty:g} -> {new_qty:g} at {stock_location.display_name} ({line_refs})"
            )

        if skipped_lines:
            summary_lines.append("Skipped ignored lines: " + ", ".join(skipped_lines))

        summary = "POS inventory sync completed:\n" + "\n".join(f"- {line}" for line in summary_lines)
        self.write(
            {
                "state": "inventory_synced",
                "error_message": False,
                "pos_inventory_sync_summary": summary,
                "pos_inventory_synced_at": fields.Datetime.now(),
            }
        )
        self._run_pos_restock_detection_from_rows(
            sync_rows,
            shopify_location_id,
            "POS inventory sync succeeded",
        )
        _logger.info("POS inventory sync completed for order %s", self.order_name)
        return True

    def _run_pos_restock_detection_from_rows(self, sync_rows, shopify_location_id, context):
        """Create restock tasks for valid POS lines, even when another line blocks stock sync."""
        if not sync_rows:
            return
        try:
            self._create_restock_detections_from_rows(sync_rows, shopify_location_id)
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "Restock detection failed for order %s while %s",
                self.order_name,
                context,
            )

    def _create_restock_detections_from_rows(self, sync_rows, shopify_location_id):
        """Flag below-threshold variants from validated rows and (re)open tasks."""
        self.ensure_one()
        if not sync_rows:
            return
        item_model = self.env["fulfillment.restock.item"].sudo()
        for row in sync_rows:
            metafields = row.get("restock_metafields") or {}
            restock_level = metafields.get("restock_level")
            desired_level = metafields.get("desired_inventory_level")
            if restock_level is None:
                continue
            try:
                restock_level_int = int(restock_level)
            except (TypeError, ValueError):
                continue
            try:
                current_qty = int(row.get("available_qty") or 0)
            except (TypeError, ValueError):
                continue
            if current_qty >= restock_level_int:
                continue

            try:
                desired_int = int(desired_level) if desired_level is not None else 0
            except (TypeError, ValueError):
                desired_int = 0
            recommended = max(desired_int - current_qty, 0) if desired_int else 0

            line = row["line"]
            shop_domain = self._get_shopify_api().shop_domain or ""
            product_url = ""
            if shop_domain and line.shopify_product_id:
                product_url = (
                    f"https://{shop_domain}/admin/products/{line.shopify_product_id}"
                )

            identity_key = item_model._compute_identity_key(
                location_piece=shopify_location_id,
                variant_id_global=line.shopify_variant_id,
                product_id_global=line.shopify_product_id,
                sku=line.sku,
                product_title=line.title,
                variant_title=line.variant_title,
            )
            item = item_model.create({
                "product_title": line.title or "",
                "variant_title": line.variant_title or "",
                "sku": line.sku or "",
                "product_url": product_url or False,
                "current_qty": current_qty,
                "restock_level": restock_level_int,
                "restock_amount": recommended,
                "product_id_global": line.shopify_product_id or "",
                "variant_id_global": line.shopify_variant_id or "",
                "shopify_location_id": str(shopify_location_id) if shopify_location_id else False,
                "identity_key": identity_key,
                "is_active_snapshot": True,
                "source_pos_order_id": self.id,
            })
            try:
                item._create_or_merge_task()
            except Exception:  # pylint: disable=broad-except
                _logger.exception(
                    "Failed to create/merge restock task for item %s (variant %s)",
                    item.id, line.shopify_variant_id,
                )

    def _get_retail_shopify_location_id_for_restock(self):
        """Best available Shopify location for retail restock checks."""
        self.ensure_one()
        location_id = self._get_shopify_pos_location_id()
        if location_id:
            return location_id

        ICP = self.env["ir.config_parameter"].sudo()
        for key in (
            "fulfillment.shopify_location_id",
            "odoo_shopify_restock.location_id_numeric",
            "odoo_shopify_restock.location_id_global",
        ):
            raw = (ICP.get_param(key) or "").strip()
            if raw:
                return raw.split("/")[-1]

        recent_pos = self.sudo().search(
            [("source", "=", "pos"), ("shopify_location_id", "!=", False)],
            order="write_date desc, id desc",
            limit=1,
        )
        return (recent_pos.shopify_location_id or "").strip()

    def _build_restock_detection_rows_for_retail_location(self, shopify_location_id):
        """Read Shopify's current retail quantity for this order's lines."""
        self.ensure_one()
        if not shopify_location_id:
            return []

        api = self._get_shopify_api()
        rows = []
        for line in self.line_ids:
            try:
                if api.product_has_true_metafield(line.shopify_product_id, "baked_goods"):
                    _logger.info(
                        "Order %s line %s skipped for restock detection: baked goods product",
                        self.order_name,
                        line.sku or line.title or line.id,
                    )
                    continue
            except Exception as exc:  # pylint: disable=broad-except
                _logger.warning(
                    "Order %s line %s restock metafield precheck failed: %s",
                    self.order_name,
                    line.sku or line.title or line.id,
                    exc,
                )
                continue

            sku = (line.sku or "").strip()
            if not line.shopify_variant_id or not sku:
                continue

            product = self._find_odoo_product_by_sku(sku)
            if not product:
                _logger.warning(
                    "Order %s line %s skipped for restock detection: no matching Odoo product",
                    self.order_name,
                    sku,
                )
                continue

            try:
                inventory_item_id = api.get_variant_inventory_item_id(line.shopify_variant_id)
                available_qty = api.get_available_inventory_quantity(
                    inventory_item_id,
                    shopify_location_id,
                )
                restock_metafields = api.get_variant_restock_metafields(
                    line.shopify_variant_id,
                    line.shopify_product_id,
                )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.warning(
                    "Order %s line %s skipped for restock detection: %s",
                    self.order_name,
                    sku,
                    exc,
                )
                continue

            rows.append({
                "line": line,
                "product": product,
                "inventory_item_id": inventory_item_id,
                "available_qty": available_qty,
                "restock_metafields": restock_metafields,
            })
        return rows

    def _run_retail_restock_detection(self):
        """Create restock tasks when Shopify retail inventory is below threshold.

        POS orders already call this as part of the POS inventory sync. Non-POS
        orders can still reduce the same Shopify retail location, so they need a
        threshold check without rewriting Odoo's POS stock quantity.
        """
        for order in self:
            if order.source == "pos":
                continue
            shopify_location_id = order._get_retail_shopify_location_id_for_restock()
            if not shopify_location_id:
                _logger.warning(
                    "Order %s skipped retail restock detection: no Shopify location",
                    order.order_name,
                )
                continue
            rows = order._build_restock_detection_rows_for_retail_location(
                shopify_location_id
            )
            order._create_restock_detections_from_rows(rows, shopify_location_id)

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

    def _refresh_shopify_risk_level(self):
        """Fetch and persist the current Shopify risk level before fulfillment."""
        self.ensure_one()
        try:
            api = self._get_shopify_api()
            risk = api.get_risk_level(self.shopify_id)
        except Exception as exc:
            _logger.exception("Failed to verify Shopify risk level for order %s", self.id)
            raise exceptions.UserError(
                "Unable to verify Shopify fraud risk. Manual review is required before fulfillment."
            ) from exc

        if risk not in ("HIGH", "MEDIUM", "LOW"):
            raise exceptions.UserError(
                f"Shopify returned an invalid fraud risk level for order {self.order_name}: {risk}"
            )

        self.sudo().write({"shopify_risk_level": risk})
        return risk

    def _is_high_risk(self):
        """Compatibility helper: any Shopify risk needing review blocks fulfillment."""
        self.ensure_one()
        return self._refresh_shopify_risk_level() in ("HIGH", "MEDIUM")

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
        pos_synced_count = 0
        skipped_count = 0
        error_count = 0
        queued_any = False

        for order_data in shopify_orders:
            shopify_id = str(order_data.get("id"))
            source = self._source_from_payload(order_data)
            
            # Check if already exists
            existing = self.search([("shopify_id", "=", shopify_id)], limit=1)
            if existing:
                if source == "pos" or existing.source == "pos":
                    try:
                        order_vals = self._prepare_order_vals_from_shopify(order_data)
                        order_vals["line_ids"] = [(5, 0, 0)] + order_vals.get("line_ids", [])
                        existing.write(order_vals)
                        if existing._sync_pos_inventory_from_shopify():
                            pos_synced_count += 1
                    except Exception as e:
                        _logger.exception("Failed to sync existing POS order %s: %s", shopify_id, e)
                        error_count += 1
                    continue

                _logger.debug("Order %s already exists, skipping", shopify_id)
                skipped_count += 1
                continue
            
            # Prepare and create order
            try:
                order_vals = self._prepare_order_vals_from_shopify(order_data)
                order = self.create(order_vals)
                imported_count += 1
                _logger.info("Imported order %s (%s)", order.order_name, shopify_id)

                if order.source == "pos":
                    if order._sync_pos_inventory_from_shopify():
                        pos_synced_count += 1
                    continue
                
                # Create/update customer in Odoo database
                try:
                    order._create_or_update_partner()
                except Exception as partner_err:
                    _logger.warning("Failed to create partner for order %s: %s", shopify_id, partner_err)
                
                # Pickup tasks must be created even when automated shipping is
                # disabled. Shipping orders retain the existing setting.
                ICP = self.env["ir.config_parameter"].sudo()
                auto_process = ICP.get_param("fulfillment.auto_process", "False")
                if order.state == "pending" and (
                    order.fulfillment_type == "pickup"
                    or auto_process.lower() in ("true", "1", "yes")
                ):
                    order.auto_process_queued = True
                    queued_any = True
                    _logger.info("Order %s queued for auto-processing", order.id)
                    
            except Exception as e:
                _logger.exception("Failed to import order %s: %s", shopify_id, e)
                error_count += 1
        
        if queued_any:
            self.trigger_queued_processing_cron()

        message = (
            "Shopify Sync Complete:\n"
            f"Imported: {imported_count}\n"
            f"POS inventory synced: {pos_synced_count}\n"
            f"Skipped existing online orders: {skipped_count}\n"
            f"Errors: {error_count}"
        )
        _logger.info(message)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Shopify Sync Complete',
                'message': f"Imported: {imported_count}, POS synced: {pos_synced_count}, Skipped: {skipped_count}, Errors: {error_count}",
                'type': 'success' if error_count == 0 else 'warning',
                'sticky': False,
            }
        }

    def _prepare_order_vals_from_shopify(self, payload: dict):
        """Prepare order values from Shopify API response (same as webhook format)."""
        shipping = payload.get("shipping_address") or {}
        shipping_line1, shipping_line2 = normalize_address_lines(
            shipping.get("address1"),
            shipping.get("address2"),
        )
        line_vals = []
        for line in payload.get("line_items", []):
            properties = {
                str(prop.get("name") or "").strip().lower(): prop.get("value")
                for prop in (line.get("properties") or [])
                if isinstance(prop, dict)
            }
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
                        "unit_price": line.get("price") or 0.0,
                        "amazon_asin": properties.get("asin") or "",
                        "requires_shipping": line.get("requires_shipping", True),
                    },
                )
            )
        source = self._source_from_payload(payload)
        
        shipping_lines = payload.get("shipping_lines") or []
        requested_method = False
        shipping_amount_paid = 0.0
        if shipping_lines:
            requested_method = (
                shipping_lines[0].get("title")
                or shipping_lines[0].get("code")
                or shipping_lines[0].get("carrier_identifier")
            )
            try:
                shipping_amount_paid = float(shipping_lines[0].get("price") or 0.0)
            except (TypeError, ValueError):
                shipping_amount_paid = 0.0

        note_attributes = {
            str(item.get("name") or "").strip().lower(): item.get("value")
            for item in (payload.get("note_attributes") or [])
            if isinstance(item, dict)
        }

        def _marketplace_datetime(name):
            value = note_attributes.get(name.lower())
            if not value:
                return False
            try:
                from dateutil import parser

                dt = parser.parse(str(value))
                if dt.tzinfo:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except (TypeError, ValueError, OverflowError):
                _logger.warning(
                    "Order %s: invalid marketplace timestamp %s=%r",
                    payload.get("name") or payload.get("id"),
                    name,
                    value,
                )
                return False

        order_currency = (
            payload.get("currency")
            or ((payload.get("current_total_price_set") or {}).get("shop_money") or {}).get(
                "currency_code"
            )
            or "USD"
        )
        
        created_at = False
        if payload.get("created_at"):
            try:
                from dateutil import parser
                dt = parser.parse(payload.get("created_at"))
                created_at = (
                    dt.astimezone(timezone.utc).replace(tzinfo=None)
                    if dt.tzinfo
                    else dt
                )
            except Exception:
                pass
        
        vals = {
            "shopify_id": str(payload.get("id")),
            "order_number": payload.get("order_number"),
            "order_name": payload.get("name"),
            "email": payload.get("email"),
            "customer_name": self._extract_customer_name_from_payload(payload),
            "shipping_address_line1": shipping_line1,
            "shipping_address_line2": shipping_line2,
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
            "order_currency": order_currency,
            "shipping_amount_paid": shipping_amount_paid,
            "amazon_order_id": note_attributes.get("amazon order id") or False,
            "amazon_earliest_ship_at": _marketplace_datetime(
                "amazon earliest ship date"
            ),
            "amazon_latest_ship_at": _marketplace_datetime("amazon latest ship date"),
            "amazon_earliest_delivery_at": _marketplace_datetime(
                "amazon earliest delivery date"
            ),
            "amazon_latest_delivery_at": _marketplace_datetime(
                "amazon latest delivery date"
            ),
            "shopify_location_id": self._shopify_location_id_from_payload(payload),
        }
        vals.update(self._fulfillment_classification_vals(payload))
        return vals

    def action_process(self):
        for order in self:
            order.process_order()

    @api.model
    def trigger_queued_processing_cron(self):
        """Ask the processing cron to run right after this transaction commits."""
        try:
            cron = self.env.ref(
                "shopify_fulfillment.ir_cron_process_queued_orders",
                raise_if_not_found=False,
            )
            if cron:
                cron.sudo()._trigger()
        except Exception:  # pylint: disable=broad-except
            # The scheduled interval run will pick the order up regardless.
            _logger.exception("Failed to trigger queued-order processing cron")

    @api.model
    def cron_process_queued_orders(self, limit=20):
        """Process orders queued by the webhook when auto-processing is on.

        The webhook only flags the order and returns immediately (Shopify
        expects a response within ~5 seconds); the actual rate shopping and
        label purchasing happens here.
        """
        orders = self.search(
            [
                ("state", "=", "pending"),
                ("auto_process_queued", "=", True),
                ("source", "!=", "pos"),
            ],
            limit=limit,
        )
        if orders:
            _logger.info("Cron: processing %d queued order(s)", len(orders))
            orders.process_order()
            # process_order moves orders out of "pending" (ready/manual/error);
            # clear the queue flag on those so state resets don't re-trigger.
            processed = orders.filtered(lambda o: o.state != "pending")
            if processed:
                processed.write({"auto_process_queued": False})

        retry_orders = self.search(
            [
                ("fulfillment_type", "=", "pickup"),
                ("state", "=", "pickup_pending"),
                ("pickup_notification_state", "in", ("queued", "error")),
                ("pickup_notification_attempts", "<", 3),
            ],
            limit=limit,
        )
        for order in retry_orders:
            order._send_pickup_notification()

    def action_reset_and_reprocess(self):
        """Refresh from Shopify, reset fulfillment artifacts, and reprocess."""
        for order in self:
            order._refresh_from_shopify_for_reprocess()
        self._reset_fulfillment_state()
        self.process_order()

    def _refresh_from_shopify_for_reprocess(self):
        """Refresh mutable order details before buying replacement labels.

        Resetting can refund labels and delete local fulfillment artifacts, so
        the Shopify fetch and address validation deliberately happen first.
        """
        self.ensure_one()

        try:
            shopify_orders = self._get_shopify_api().get_orders([self.shopify_id])
        except Exception as exc:  # pylint: disable=broad-except
            raise exceptions.UserError(
                f"Reset stopped: Shopify order {self.order_name} could not be refreshed: {exc}"
            ) from exc

        order_data = next(
            (
                data
                for data in shopify_orders
                if str(data.get("id")) == str(self.shopify_id)
            ),
            None,
        )
        if not order_data:
            raise exceptions.UserError(
                f"Reset stopped: Shopify did not return order {self.order_name}."
            )

        prepared = self._prepare_order_vals_from_shopify(order_data)
        refresh_fields = (
            "email",
            "customer_name",
            "shipping_address_line1",
            "shipping_address_line2",
            "shipping_city",
            "shipping_state",
            "shipping_zip",
            "shipping_country",
            "shipping_phone",
            "raw_payload",
            "requested_shipping_method",
            "order_currency",
            "shipping_amount_paid",
            "amazon_order_id",
            "amazon_earliest_ship_at",
            "amazon_latest_ship_at",
            "amazon_earliest_delivery_at",
            "amazon_latest_delivery_at",
            "source",
            "shopify_location_id",
        )
        refresh_vals = {
            field_name: prepared.get(field_name) or False
            for field_name in refresh_fields
        }

        required_address_fields = {
            "street": refresh_vals["shipping_address_line1"],
            "city": refresh_vals["shipping_city"],
            "state": refresh_vals["shipping_state"],
            "ZIP/postal code": refresh_vals["shipping_zip"],
            "country": refresh_vals["shipping_country"],
        }
        missing = [
            label for label, value in required_address_fields.items() if not value
        ]
        if missing:
            raise exceptions.UserError(
                f"Reset stopped: Shopify order {self.order_name} still has no usable "
                f"shipping address. Missing: {', '.join(missing)}."
            )

        self.write(refresh_vals)
        _logger.info(
            "Order %s: refreshed customer, shipping address, and shipping method from Shopify before reset",
            self.id,
        )
        return order_data

    def _reset_fulfillment_state(self):
        """Clear shipments/print jobs and return order to pending state."""
        for order in self:
            if order.state == "processing":
                raise exceptions.UserError(
                    "Order is currently processing. Please wait or set to error before resetting."
                )

            group = order.shipment_group_id
            group_id = group.id if group else False
            single_shipment = order.shipment_id
            single_group_id = single_shipment.group_id.id if single_shipment else False
            shipments_to_refund = self.env["fulfillment.shipment"]
            if group:
                shipments_to_refund |= group.shipment_ids
            if single_shipment:
                shipments_to_refund |= single_shipment

            order._request_label_cancellations_for_shipments(shipments_to_refund)

            if order.print_job_ids:
                # Print jobs are not unlinkable by default users, so elevate for cleanup.
                order.print_job_ids.sudo().unlink()

            order.write(
                {
                    "shipment_id": False,
                    "shipment_group_id": False,
                    "box_id": False,
                    "state": "pending",
                    "error_message": False,
                    "active": True,
                }
            )

            if group_id:
                group.unlink()

            if single_shipment and (not group_id or single_group_id != group_id):
                if single_shipment.exists():
                    single_shipment.unlink()

    def _request_label_cancellations_for_shipments(self, shipments):
        """Cancel/refund labels through the provider that sold each label."""
        self.ensure_one()
        ambiguous = shipments.filtered(
            lambda shipment: shipment.purchase_state
            in ("pending", "submitting", "uncertain")
            or shipment.refund_status == "uncertain"
        )
        if ambiguous:
            identifiers = ", ".join(
                shipment.tracking_number
                or shipment.provider_shipment_id
                or str(shipment.id)
                for shipment in ambiguous
            )
            raise exceptions.UserError(
                "Reset stopped because these label purchases must be reconciled first: "
                + identifiers
            )

        amazon_shipments = shipments.filtered(
            lambda shipment: shipment.purchase_provider == "amazon_shipping"
        )
        if amazon_shipments:
            from odoo.addons.shopify_fulfillment.services.amazon_shipping_service import (
                AmazonShippingService,
            )

            amazon = AmazonShippingService.from_env(
                self.env, require_enabled=False
            )
            if not amazon:
                raise exceptions.UserError(
                    "Amazon Shipping credentials are unavailable. Amazon labels were not "
                    "cancelled and the reset was stopped."
                )
            failures = []
            for shipment in amazon_shipments:
                if shipment.refund_status in ("queued", "pending", "success"):
                    continue
                if not shipment.provider_shipment_id:
                    failures.append(
                        f"{shipment.tracking_number or shipment.id}: missing Amazon shipment ID"
                    )
                    continue
                result = amazon.cancel_shipment(shipment.provider_shipment_id)
                if result.get("error"):
                    cancel_status = (
                        "uncertain"
                        if result.get("status") == "uncertain"
                        else "error"
                    )
                    shipment.write({
                        "refund_status": cancel_status,
                        "refund_error_message": result.get("error"),
                    })
                    failures.append(
                        f"{shipment.tracking_number or shipment.provider_shipment_id}: "
                        f"{result.get('error')}"
                    )
                    continue
                shipment.write({
                    "refund_status": "success",
                    "refund_requested_at": fields.Datetime.now(),
                    "refund_error_message": False,
                    "provider_cancel_reference": shipment.provider_shipment_id,
                })
            if failures:
                raise exceptions.UserError(
                    "Reset stopped because not all Amazon labels could be cancelled:\n- "
                    + "\n- ".join(failures)
                )

        shippo_shipments = shipments.filtered(
            lambda shipment: shipment.purchase_provider == "shippo"
            or (
                not shipment.purchase_provider
                and bool(shipment.shippo_transaction_id)
            )
        )
        if shippo_shipments:
            self._request_shippo_refunds_for_shipments(shippo_shipments)

        unsupported = shipments - amazon_shipments - shippo_shipments
        unsupported = unsupported.filtered(
            lambda shipment: shipment.tracking_number or shipment.label_zpl
        )
        if unsupported:
            raise exceptions.UserError(
                "Reset stopped because one or more labels have no supported cancellation provider."
            )

    def _request_shippo_refunds_for_shipments(self, shipments):
        """Request Shippo refunds before resetting local shipment records."""
        self.ensure_one()
        shipments = shipments.filtered(
            lambda shipment: shipment.tracking_number
            or shipment.label_url
            or shipment.shippo_transaction_id
        )
        if not shipments:
            return

        refundable_shipments = shipments.filtered(
            lambda shipment: shipment.refund_status not in ("queued", "pending", "success")
        )
        if not refundable_shipments:
            _logger.info(
                "Order %s: Existing labels already have refund requests recorded",
                self.id,
            )
            return

        from odoo.addons.shopify_fulfillment.services.shippo_service import ShippoService

        shippo = ShippoService.from_env(self.env)
        if not shippo:
            raise exceptions.UserError(
                "Shippo API key is not configured. Existing labels were not cleared "
                "because Odoo could not request refunds first."
            )

        tracking_numbers = [
            shipment.tracking_number
            for shipment in refundable_shipments
            if shipment.tracking_number and not shipment.shippo_transaction_id
        ]
        transactions_by_tracking = shippo.find_transactions_by_tracking_numbers(
            tracking_numbers,
        )

        failures = []
        for shipment in refundable_shipments:
            transaction_id = (shipment.shippo_transaction_id or "").strip()
            if not transaction_id and shipment.tracking_number:
                transaction = transactions_by_tracking.get(shipment.tracking_number)
                transaction_id = (transaction or {}).get("object_id")
                if transaction_id:
                    shipment.write({"shippo_transaction_id": transaction_id})

            if not transaction_id:
                failures.append(
                    f"{shipment.tracking_number or shipment.id}: missing Shippo transaction ID"
                )
                continue

            refund = shippo.refund_label(transaction_id)
            if refund.get("error"):
                error_message = refund.get("error") or "Unknown Shippo refund error"
                shipment.write(
                    {
                        "refund_status": "error",
                        "refund_error_message": error_message,
                    }
                )
                failures.append(
                    f"{shipment.tracking_number or transaction_id}: {error_message}"
                )
                continue

            status = (refund.get("status") or "QUEUED").lower()
            if status not in ("queued", "pending", "success"):
                status = "error"

            shipment.write(
                {
                    "shippo_refund_id": refund.get("object_id"),
                    "refund_status": status,
                    "refund_requested_at": fields.Datetime.now(),
                    "refund_error_message": False,
                }
            )

            if status == "error":
                failures.append(
                    f"{shipment.tracking_number or transaction_id}: refund rejected"
                )

        if failures:
            raise exceptions.UserError(
                "Reset stopped before deleting local shipment records because not all "
                "existing labels could be refunded:\n- "
                + "\n- ".join(failures)
            )

    def process_order(self):
        """End-to-end flow: box selection, rate shopping, label purchase, print job."""
        for order in self:
            try:
                try:
                    order._run_retail_restock_detection()
                except Exception:  # pylint: disable=broad-except
                    _logger.exception(
                        "Retail restock detection failed for order %s",
                        order.order_name,
                    )

                # Ensure customer is in Odoo database before processing
                try:
                    order._create_or_update_partner()
                except Exception as partner_err:
                    _logger.warning("Failed to create partner for order %s during process: %s", order.id, partner_err)
                
                order._process_order_inner()
            except ShippingPolicyHold as exc:
                _logger.warning("Shipping policy held order %s: %s", order.id, exc)
                order.write({"state": "manual_required", "error_message": str(exc)})
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Order processing failed for %s", order.id)
                order.write({"state": "error", "error_message": str(exc)})

    def _ensure_amazon_shipping_metadata(self):
        """Backfill marketplace promise fields for orders created before v0.5."""
        self.ensure_one()
        if self.source != "amazon" or not self.raw_payload:
            return
        if self.amazon_order_id and self.amazon_latest_delivery_at:
            return
        try:
            payload = json.loads(self.raw_payload)
        except (TypeError, ValueError):
            return
        attributes = {
            str(item.get("name") or "").strip().lower(): item.get("value")
            for item in (payload.get("note_attributes") or [])
            if isinstance(item, dict)
        }
        updates = {}
        mapping = {
            "amazon_order_id": "amazon order id",
            "amazon_earliest_ship_at": "amazon earliest ship date",
            "amazon_latest_ship_at": "amazon latest ship date",
            "amazon_earliest_delivery_at": "amazon earliest delivery date",
            "amazon_latest_delivery_at": "amazon latest delivery date",
        }
        for field_name, attribute_name in mapping.items():
            if getattr(self, field_name):
                continue
            value = attributes.get(attribute_name)
            if not value:
                continue
            updates[field_name] = (
                value
                if field_name == "amazon_order_id"
                else self._provider_datetime(value)
            )
        if updates:
            self.sudo().write(updates)

    def _send_error_alert(self, title: str, message: str, extra: Optional[dict] = None):
        self.ensure_one()
        try:
            from odoo.addons.shopify_fulfillment.services.alert_service import AlertService

            AlertService.from_env(self.env).notify_error(
                title=title,
                message=message,
                order=self,
                extra=extra,
            )
        except Exception as alert_exc:  # pylint: disable=broad-except
            _logger.exception("Order %s: failed to send error alert: %s", self.id, alert_exc)

    def write(self, vals):
        tracked = None
        alert_state = vals.get("state")
        if alert_state in ("error", "manual_required"):
            tracked = {order.id: order.state for order in self}

        res = super().write(vals)

        if tracked:
            alert_message = vals.get("error_message")
            for order in self:
                previous_state = tracked.get(order.id)
                if previous_state == alert_state:
                    continue
                if alert_state == "error":
                    title = "Order Processing Error"
                    fallback_message = "Order moved to error state."
                else:
                    title = "Order Requires Manual Review"
                    fallback_message = "Order moved to Manual Review and was not shipped automatically."

                message = alert_message or order.error_message or fallback_message
                order._send_error_alert(
                    title=title,
                    message=message,
                    extra={
                        "previous_state": previous_state or "-",
                        "new_state": alert_state,
                    },
                )

        return res

    def _process_order_inner(self):
        self.ensure_one()

        if self.source == "pos":
            self._sync_pos_inventory_from_shopify()
            return

        if self.fulfillment_type == "pickup":
            self._process_pickup_order()
            return

        self._ensure_amazon_shipping_metadata()
        if self.source == "amazon" and not self.amazon_latest_delivery_at:
            raise ShippingPolicyHold(
                "Amazon's required delivery deadline is missing. Review the "
                "marketplace order before buying a label."
            )
        if (
            self.source == "amazon"
            and self.amazon_latest_ship_at
            and fields.Datetime.now() > self.amazon_latest_ship_at
        ):
            raise ShippingPolicyHold(
                "Amazon's latest ship-by time has passed. Review the order before buying a label."
            )

        # Step 0: Risk Check
        try:
            risk_level = self._refresh_shopify_risk_level()
        except exceptions.UserError as exc:
            self.write({
                "state": "manual_required",
                "error_message": str(exc),
            })
            return

        if risk_level in ("HIGH", "MEDIUM"):
            _logger.warning("Order %s flagged as %s risk. Stopping processing.", self.id, risk_level)
            self.write({
                "state": "manual_required",
                "error_message": f"Flagged by Shopify as {risk_level.title()} Risk. Manual review required before fulfillment.",
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
            # Only take the reprint shortcut when the previous run finished
            # every box; a mid-run failure leaves group.state != "complete"
            # with labels for only some boxes, and reprinting just those
            # would ship the order incomplete.
            group_is_complete = (
                group.state == "complete"
                and shipments_with_labels
                and len(shipments_with_labels) == len(group.shipment_ids)
            )

            if group_is_complete:
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
                # Previous processing failed or stopped partway. Refund any
                # labels that were purchased, drop their queued print jobs,
                # then rebuild the whole group from scratch.
                _logger.info(
                    "Order %s: Shipment group %s is incomplete (state=%s, %d/%d labeled); "
                    "refunding and reprocessing",
                    self.id,
                    group.id,
                    group.state,
                    len(shipments_with_labels),
                    len(group.shipment_ids),
                )
                self._request_label_cancellations_for_shipments(group.shipment_ids)
                stale_jobs = self.print_job_ids.filtered(
                    lambda j: j.shipment_id in group.shipment_ids
                )
                if stale_jobs:
                    stale_jobs.sudo().unlink()
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
        shipping_service = shippo

        if self.source == "amazon":
            from odoo.addons.shopify_fulfillment.services.amazon_shipping_service import (
                AmazonShippingService,
            )

            amazon_shipping = AmazonShippingService.from_env(self.env)
            if amazon_shipping:
                shipping_service = amazon_shipping
            else:
                amazon_enabled = AmazonShippingService._truthy(
                    self.env["ir.config_parameter"].sudo().get_param(
                        "amazon_shipping.enabled", "False"
                    )
                )
                if amazon_enabled:
                    raise exceptions.UserError(
                        "Amazon Buy Shipping is enabled but its LWA credentials are incomplete."
                    )

        # Process each packed box
        shipments_created = []
        for sequence, packed_box in enumerate(packing_result.packed_boxes, start=1):
            try:
                shipment = self._process_single_box(
                    packed_box=packed_box,
                    group=group,
                    sequence=sequence,
                    shipping_service=shipping_service,
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

        deferred_shipments = group.shipment_ids.filtered(
            lambda shipment: shipment.purchase_state
            in ("pending", "submitting")
        )
        if deferred_shipments:
            group.write({"state": "pending"})
            if shipments_created:
                self.shipment_id = shipments_created[0].id
                self.box_id = shipments_created[0].box_id.id
            _logger.info(
                "Order %s: committed %d Amazon purchase intent(s); labels will be "
                "purchased post-commit",
                self.id,
                len(deferred_shipments),
            )
            return

        # Update group state
        group.write({"state": "complete"})

        # Queue print jobs only after every box purchased successfully, so a
        # mid-run failure never prints labels for a partially processed order.
        for shipment in shipments_created:
            self.env["print.job"].create({
                "order_id": self.id,
                "shipment_id": shipment.id,
                "job_type": "label",
                "zpl_data": shipment.label_zpl or "",
                "printer_id": False,
            })

        # Backward compatibility: set shipment_id and box_id to first shipment
        if shipments_created:
            self.shipment_id = shipments_created[0].id
            self.box_id = shipments_created[0].box_id.id

        _logger.info("Order %s: Created %d shipments (multi-box)", self.id, len(shipments_created))
        self.write({"state": "ready_to_ship"})

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

    @staticmethod
    def _provider_datetime(value):
        """Convert provider ISO timestamps to Odoo's naive UTC datetime."""
        if not value:
            return False
        if hasattr(value, "tzinfo"):
            parsed = value
        else:
            try:
                from dateutil import parser

                parsed = parser.parse(str(value))
            except (TypeError, ValueError, OverflowError):
                return False
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _register_amazon_purchase_postcommit(self, shipment_id):
        """Run Amazon purchase only after its local intent has committed."""
        self.ensure_one()
        database_name = self.env.cr.dbname
        order_id = self.id

        def _purchase_after_commit():
            from odoo import SUPERUSER_ID, api as odoo_api, registry

            with registry(database_name).cursor() as cursor:
                callback_env = odoo_api.Environment(cursor, SUPERUSER_ID, {})
                callback_order = callback_env["shopify.order"].browse(order_id)
                try:
                    callback_order._execute_amazon_purchase_intent(shipment_id)
                    cursor.commit()
                except Exception:  # pylint: disable=broad-except
                    _logger.exception(
                        "Amazon post-commit purchase failed for shipment intent %s",
                        shipment_id,
                    )
                    shipment = callback_env["fulfillment.shipment"].browse(
                        shipment_id
                    )
                    if shipment.exists() and shipment.purchase_state == "submitting":
                        shipment.write({"purchase_state": "uncertain"})
                        shipment.order_id.write({
                            "state": "manual_required",
                            "error_message": (
                                "Amazon label purchase stopped in an uncertain state. "
                                "Reconcile in Amazon before retrying."
                            ),
                        })
                    cursor.commit()

        self.env.cr.postcommit.add(_purchase_after_commit)

    def _execute_amazon_purchase_intent(self, shipment_id):
        """Execute one committed Amazon label intent without automatic retry."""
        self.ensure_one()
        shipment = self.env["fulfillment.shipment"].browse(shipment_id).exists()
        if not shipment or shipment.order_id != self:
            return
        if shipment.purchase_state != "pending":
            return
        if shipment.group_id.state == "error" or self.state in (
            "error",
            "manual_required",
        ):
            shipment.write({"purchase_state": "error"})
            return

        try:
            bundle = json.loads(shipment.provider_payload_json or "{}")
        except (TypeError, ValueError):
            shipment.write({"purchase_state": "error"})
            self.write({
                "state": "manual_required",
                "error_message": "Amazon purchase intent payload is invalid.",
            })
            return

        selected_rate = bundle.get("selected_rate") or {}
        rates = bundle.get("rates") or []
        rate_meta = bundle.get("rate_meta") or {}
        selection_details = bundle.get("selection_details") or {}

        # This isolated post-commit cursor deliberately commits the inflight
        # marker before the external financial mutation. A worker crash can
        # strand an intent for reconciliation, but cannot silently retry it.
        shipment.write({"purchase_state": "submitting"})
        self.env.cr.commit()

        from odoo.addons.shopify_fulfillment.services.amazon_shipping_service import (
            AmazonShippingPurchaseUncertain,
            AmazonShippingService,
        )

        amazon = AmazonShippingService.from_env(self.env)
        if not amazon:
            shipment.write({"purchase_state": "error"})
            self.write({
                "state": "manual_required",
                "error_message": "Amazon Shipping credentials are unavailable.",
            })
            return

        try:
            shipment_vals = amazon.purchase_label(selected_rate)
        except AmazonShippingPurchaseUncertain as exc:
            shipment.write({
                "purchase_state": "uncertain",
                "provider_shipment_id": exc.shipment_id or False,
                "provider_response_json": json.dumps(
                    exc.response_data or {}, sort_keys=True
                ),
            })
            self.shipment_group_id.write({"state": "error"})
            self.write({
                "state": "manual_required",
                "error_message": str(exc),
            })
            return
        except Exception as exc:  # pylint: disable=broad-except
            shipment.write({"purchase_state": "error"})
            self.shipment_group_id.write({"state": "error"})
            self.write({
                "state": "manual_required",
                "error_message": str(exc),
            })
            return

        if not shipment_vals or shipment_vals.get("error"):
            shipment.write({"purchase_state": "error"})
            self.shipment_group_id.write({"state": "error"})
            self.write({
                "state": "manual_required",
                "error_message": (
                    (shipment_vals or {}).get("error")
                    or "Amazon label purchase returned no result."
                ),
            })
            return

        shipment.write({
            "purchase_state": "purchased",
            "provider_shipment_id": shipment_vals.get("provider_shipment_id"),
            "provider_rate_id": shipment_vals.get("provider_rate_id")
            or shipment.provider_rate_id,
            "provider_service_id": shipment_vals.get("provider_service_id")
            or shipment.provider_service_id,
            "provider_response_json": shipment_vals.get("provider_response_json"),
            "tracking_number": shipment_vals.get("tracking_number"),
            "tracking_url": shipment_vals.get("tracking_url"),
            "label_url": shipment_vals.get("label_url"),
            "label_zpl": shipment_vals.get("label_zpl"),
            "label_format": shipment_vals.get("label_format") or "ZPL",
            "carrier": shipment_vals.get("carrier"),
            "service": shipment_vals.get("service"),
            "rate_amount": shipment_vals.get("rate_amount"),
            "rate_currency": shipment_vals.get("rate_currency"),
            "promised_delivery_at": self._provider_datetime(
                shipment_vals.get("promised_delivery_at")
            ),
            "purchased_at": fields.Datetime.now(),
        })

        try:
            self.env["fulfillment.rate.audit"].sudo().log_purchase(
                order=self,
                shipment=shipment,
                group=shipment.group_id,
                sequence=shipment.sequence,
                weight_grams=shipment.total_weight,
                rates=rates,
                selected_rate=selected_rate,
                is_residential=rate_meta.get("is_residential"),
                rate_meta=rate_meta,
                selection=selection_details,
            )
        except Exception:
            _logger.exception(
                "Order %s: failed to record Amazon rate audit",
                self.id,
            )

        self._finalize_deferred_purchase_group()

    def _finalize_deferred_purchase_group(self):
        self.ensure_one()
        group = self.shipment_group_id
        if not group:
            return
        shipments = group.shipment_ids
        if shipments.filtered(lambda shipment: shipment.purchase_state in ("error", "uncertain")):
            group.write({"state": "error"})
            self.write({"state": "manual_required"})
            return
        if shipments.filtered(
            lambda shipment: shipment.purchase_state != "purchased"
        ):
            group.write({"state": "partial"})
            return

        group.write({"state": "complete"})
        for shipment in shipments:
            existing_job = self.print_job_ids.filtered(
                lambda job: job.shipment_id == shipment and job.job_type == "label"
            )
            if not existing_job:
                self.env["print.job"].create({
                    "order_id": self.id,
                    "shipment_id": shipment.id,
                    "job_type": "label",
                    "zpl_data": shipment.label_zpl or "",
                    "printer_id": False,
                })
        first_shipment = shipments.sorted(lambda shipment: (shipment.sequence, shipment.id))[:1]
        if first_shipment:
            self.shipment_id = first_shipment.id
            self.box_id = first_shipment.box_id.id
        self.write({"state": "ready_to_ship", "error_message": False})

    def _process_single_box(self, packed_box, group, sequence: int, shipping_service) -> Optional[models.Model]:
        """Process a single box: rate shop, purchase label, create shipment, print job.

        Args:
            packed_box: PackedBox instance from packer
            group: fulfillment.shipment.group record
            sequence: Box number (1, 2, 3...)
            shipping_service: ShippoService or AmazonShippingService instance

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
        rate_meta = {
            "is_residential": None,
            "validation_results": None,
            "provider": "mock",
        }
        selection_details = {}
        provider_name = getattr(shipping_service, "provider_name", "mock")

        if shipping_service:
            if provider_name == "amazon_shipping":
                rates, rate_meta = shipping_service.get_rates_for_box(
                    order=self,
                    packed_box=packed_box,
                    box=box_record,
                    total_weight_grams=packed_box.total_weight_with_box,
                    sender_company=self.env.company,
                    sequence=sequence,
                )
            else:
                rates, rate_meta = shipping_service.get_rates_for_box(
                    order=self,
                    box=box_record,
                    total_weight_grams=packed_box.total_weight_with_box,
                    sender_company=self.env.company,
                )
                rate_meta["provider"] = "shippo"

            # Exclusions use stable service/carrier IDs, never display names.
            config_params = self.env["ir.config_parameter"].sudo()
            excluded_service_ids = {
                value.strip()
                for value in config_params.get_param(
                    "fulfillment.excluded_service_ids",
                    "ups_ground_saver,UPS_PTP_GROUNDSAVER",
                ).split(",")
                if value.strip()
            }
            excluded_carrier_ids = {
                value.strip()
                for value in config_params.get_param(
                    "fulfillment.excluded_carrier_ids", ""
                ).split(",")
                if value.strip()
            }
            original_count = len(rates)

            def _rate_is_allowed(rate):
                amazon_meta = rate.get("_amazon") or {}
                service_id = (rate.get("servicelevel") or {}).get("token")
                carrier_id = rate.get("carrier_account") or amazon_meta.get(
                    "carrier_id"
                )
                return (
                    service_id not in excluded_service_ids
                    and carrier_id not in excluded_carrier_ids
                )

            rates = [rate for rate in rates if _rate_is_allowed(rate)]
            if original_count != len(rates):
                _logger.info(
                    "Order %s Box %d: Filtered out %d excluded services",
                    self.id,
                    sequence,
                    original_count - len(rates),
                )

            if not rates:
                raise exceptions.UserError(
                    f"Box {sequence}: {provider_name} returned no usable rates"
                )

            selected_rate, selection_details = self._select_shipping_rate(
                rates,
                return_details=True,
                audit_context={
                    "group": group,
                    "sequence": sequence,
                    "weight_grams": packed_box.total_weight_with_box,
                    "is_residential": rate_meta.get("is_residential"),
                    "rate_meta": rate_meta,
                },
            )
        else:
            # No shipping service configured. Only use the mock API when
            # explicitly enabled for testing.
            allow_mock = self.env["ir.config_parameter"].sudo().get_param(
                "fulfillment.allow_mock_api", "False"
            )
            if allow_mock.lower() not in ("true", "1", "yes"):
                raise exceptions.UserError(
                    f"Box {sequence}: no shipping label provider is configured."
                )
            api_client = self._get_shopify_api()
            rates = api_client.get_shipping_rates(self)
            if not rates:
                raise exceptions.UserError(f"Box {sequence}: Mock API returned no rates")
            selected_rate = sorted(rates, key=lambda rate: rate.get("amount", 0))[0]
            selection_details = {
                "policy_version": "mock",
                "reason": "mock_cheapest",
                "cheapest_eligible_amount": selected_rate.get("amount") or 0,
                "rejection_summary": "[]",
            }

        selected_servicelevel = selected_rate.get("servicelevel") or {}
        selected_amazon = selected_rate.get("_amazon") or {}
        shipment = self.env["fulfillment.shipment"].create({
            "order_id": self.id,
            "group_id": group.id,
            "box_id": box_record.id,
            "sequence": sequence,
            "line_ids": [(6, 0, line_ids)],
            "line_quantities": json.dumps(packed_box.line_quantities),
            "total_weight": packed_box.total_weight_with_box,
            "purchase_provider": provider_name,
            "provider_rate_id": selected_rate.get("object_id") or selected_rate.get("id"),
            "provider_service_id": selected_servicelevel.get("token"),
            "purchase_state": "pending",
            "carrier": selected_rate.get("provider"),
            "service": selected_servicelevel.get("name"),
            "rate_amount": selected_rate.get("amount") or 0.0,
            "rate_currency": selected_rate.get("currency") or "USD",
            "promised_delivery_at": self._provider_datetime(
                selected_rate.get("arrives_by")
            ),
            "label_format": (
                (selected_amazon.get("document_spec") or {}).get("format")
                if selected_amazon
                else "ZPLII"
            ),
        })
        selected_rate["_purchase_reference"] = (
            f"{self.order_name or self.id}-box-{sequence}-intent-{shipment.id}"
        )
        shipment.write({
            "provider_purchase_reference": (
                selected_amazon.get("package_reference")
                or selected_rate["_purchase_reference"]
            ),
            "provider_payload_json": json.dumps(
                {
                    "selected_rate": selected_rate,
                    "rates": rates,
                    "rate_meta": rate_meta,
                    "selection_details": selection_details,
                },
                sort_keys=True,
            ),
        })

        if provider_name == "amazon_shipping":
            self._register_amazon_purchase_postcommit(shipment.id)
            return shipment

        try:
            if shipping_service:
                shipment_vals = shipping_service.purchase_label(selected_rate)
            else:
                shipment_vals = api_client.purchase_label(
                    self, selected_rate.get("id")
                )
        except Exception as exc:
            from odoo.addons.shopify_fulfillment.services.amazon_shipping_service import (
                AmazonShippingPurchaseUncertain,
            )

            if isinstance(exc, AmazonShippingPurchaseUncertain):
                shipment.write({"purchase_state": "uncertain"})
            else:
                shipment.write({"purchase_state": "error"})
            raise

        # Carrier fallback: If USPS fails with address validation, try UPS.
        if shipment_vals and shipment_vals.get("purchase_uncertain"):
            shipment.write({
                "purchase_state": "uncertain",
                "shippo_transaction_id": shipment_vals.get("shippo_transaction_id"),
                "tracking_number": shipment_vals.get("tracking_number"),
                "tracking_url": shipment_vals.get("tracking_url"),
                "label_url": shipment_vals.get("label_url"),
                "label_zpl": shipment_vals.get("label_zpl"),
            })
            raise ShippingPolicyHold(
                f"Box {sequence}: label purchase status is uncertain. Reconcile with "
                "the provider before retrying."
            )

        if provider_name == "shippo" and shipment_vals and shipment_vals.get("error"):
            error_codes = shipment_vals.get("error_codes", [])
            failed_carrier = shipment_vals.get("failed_carrier", "")
            is_address_error = "failed_address_validation" in error_codes
            is_usps = failed_carrier.upper() == "USPS" or "USPS" in selected_rate.get(
                "provider", ""
            )

            if is_address_error and is_usps:
                _logger.warning(
                    "Order %s Box %d: USPS address validation failed, attempting UPS fallback",
                    self.id,
                    sequence,
                )
                ups_rates = [
                    rate
                    for rate in rates
                    if rate.get("provider", "").upper() == "UPS"
                ]
                if ups_rates:
                    ups_rate, selection_details = self._select_shipping_rate(
                        ups_rates, return_details=True
                    )
                    _logger.info(
                        "Order %s Box %d: Trying UPS %s at $%s",
                        self.id,
                        sequence,
                        ups_rate.get("servicelevel", {}).get("name"),
                        ups_rate.get("amount"),
                    )
                    shipment.write({
                        "provider_rate_id": ups_rate.get("object_id"),
                        "provider_service_id": (ups_rate.get("servicelevel") or {}).get("token"),
                        "carrier": ups_rate.get("provider"),
                        "service": (ups_rate.get("servicelevel") or {}).get("name"),
                        "rate_amount": ups_rate.get("amount") or 0.0,
                    })
                    ups_rate["_purchase_reference"] = (
                        f"{self.order_name or self.id}-box-{sequence}-intent-{shipment.id}"
                    )
                    shipment_vals = shipping_service.purchase_label(ups_rate)
                    if shipment_vals and not shipment_vals.get("error"):
                        selected_rate = ups_rate
                        _logger.info(
                            "Order %s Box %d: UPS fallback successful!",
                            self.id,
                            sequence,
                        )
                else:
                    _logger.warning(
                        "Order %s Box %d: No UPS rates available for fallback",
                        self.id,
                        sequence,
                    )
        if shipment_vals and shipment_vals.get("purchase_uncertain"):
            shipment.write({
                "purchase_state": "uncertain",
                "shippo_transaction_id": shipment_vals.get("shippo_transaction_id"),
                "tracking_number": shipment_vals.get("tracking_number"),
                "tracking_url": shipment_vals.get("tracking_url"),
                "label_url": shipment_vals.get("label_url"),
                "label_zpl": shipment_vals.get("label_zpl"),
            })
            raise ShippingPolicyHold(
                f"Box {sequence}: fallback label purchase status is uncertain. "
                "Reconcile with the provider before retrying."
            )
        if not shipment_vals:
            shipment.write({"purchase_state": "error"})
            raise exceptions.UserError(f"Box {sequence}: Label purchase failed (unknown error)")

        if shipment_vals.get("error"):
            shipment.write({"purchase_state": "error"})
            raise exceptions.UserError(f"Box {sequence}: {shipment_vals['error']}")

        shipment.write({
            "purchase_state": "purchased",
            "purchase_provider": shipment_vals.get("purchase_provider") or provider_name,
            "provider_shipment_id": shipment_vals.get("provider_shipment_id"),
            "provider_rate_id": shipment_vals.get("provider_rate_id") or shipment.provider_rate_id,
            "provider_service_id": shipment_vals.get("provider_service_id") or shipment.provider_service_id,
            "provider_response_json": shipment_vals.get("provider_response_json"),
            "shippo_transaction_id": shipment_vals.get("shippo_transaction_id"),
            "carrier": shipment_vals.get("carrier"),
            "service": shipment_vals.get("service"),
            "tracking_number": shipment_vals.get("tracking_number"),
            "tracking_url": shipment_vals.get("tracking_url"),
            "label_url": shipment_vals.get("label_url"),
            "label_zpl": shipment_vals.get("label_zpl"),
            "rate_amount": shipment_vals.get("rate_amount"),
            "rate_currency": shipment_vals.get("rate_currency"),
            "label_format": shipment_vals.get("label_format") or shipment.label_format,
            "promised_delivery_at": self._provider_datetime(
                shipment_vals.get("promised_delivery_at")
            ) or shipment.promised_delivery_at,
            "purchased_at": fields.Datetime.now(),
        })

        # Log rate audit row (top-3 cheapest vs. selected) for weekly review.
        try:
            self.env["fulfillment.rate.audit"].sudo().log_purchase(
                order=self,
                shipment=shipment,
                group=group,
                sequence=sequence,
                weight_grams=packed_box.total_weight_with_box,
                rates=rates,
                selected_rate=selected_rate,
                is_residential=rate_meta.get("is_residential"),
                rate_meta=rate_meta,
                selection=selection_details,
            )
        except Exception:
            _logger.exception(
                "Order %s Box %d: Failed to write rate audit row (continuing)",
                self.id,
                sequence,
            )

        _logger.info(
            "Order %s Box %d: Shipment created - %s %s",
            self.id,
            sequence,
            shipment.tracking_number,
            shipment.carrier,
        )
        return shipment

    @staticmethod
    def _normalize_shipping_text(value: str) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        normalized = normalized.encode("ascii", "ignore").decode("ascii")
        normalized = normalized.lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _shipping_speed_class(cls, normalized_value: str) -> Optional[str]:
        if not normalized_value:
            return None

        if re.search(r"\b(overnight|next day|nextday|1 day|one day|priority overnight|first overnight)\b", normalized_value):
            return "overnight"
        if re.search(r"\b(2 day|two day|2nd day|second day|48 hour)\b", normalized_value):
            return "two_day"
        if re.search(r"\b(3 day|three day|3rd day|third day|72 hour)\b", normalized_value):
            return "three_day"
        if re.search(r"\b(express|expedited|rush)\b", normalized_value):
            return "expedited"
        if re.search(r"\b(priority)\b", normalized_value):
            return "expedited"
        if re.search(r"\b(ground|standard|economy|saver|surepost|smartpost|free)\b", normalized_value):
            return "ground"
        return None

    def _requested_shipping_context(self) -> dict:
        snippets = [self.requested_shipping_method or ""]
        if self.raw_payload:
            try:
                payload = json.loads(self.raw_payload)
                shipping_lines = payload.get("shipping_lines") or []
                if shipping_lines:
                    line = shipping_lines[0] or {}
                    for key in ("title", "code", "carrier_identifier", "source"):
                        value = line.get(key)
                        if isinstance(value, str) and value.strip():
                            snippets.append(value)
            except Exception:
                _logger.debug(
                    "Order %s: unable to parse raw payload for shipping-line hints",
                    self.id,
                )

        unique_snippets = []
        seen = set()
        for snippet in snippets:
            normalized_snippet = self._normalize_shipping_text(snippet)
            if not normalized_snippet or normalized_snippet in seen:
                continue
            seen.add(normalized_snippet)
            unique_snippets.append(snippet.strip())

        merged = " ".join(unique_snippets).strip()
        normalized = self._normalize_shipping_text(merged)
        speed_class = self._shipping_speed_class(normalized)
        return {
            "raw": merged,
            "normalized": normalized,
            "normalized_options": tuple(seen),
            "speed_class": speed_class,
        }

    def _shipping_policy_for_request(self) -> dict:
        """Resolve a checkout option to structured SLA/token policy.

        Carrier display names never participate.  Optional configuration maps a
        checkout title/code to stable carrier/service IDs or a maximum transit
        time.  Amazon orders use their marketplace delivery deadline instead.
        """
        self.ensure_one()
        context = self._requested_shipping_context()
        speed_days = {
            "overnight": 1,
            "two_day": 2,
            "three_day": 3,
            "expedited": 3,
        }
        policy = {
            "allowed_carrier_ids": None,
            "allowed_service_ids": None,
            "max_estimated_days": speed_days.get(context.get("speed_class")),
            "matched_configuration": False,
            "requires_mapping": bool(
                self.requested_shipping_method
                and not context.get("speed_class")
                and self.source != "amazon"
            ),
        }

        raw = self.env["ir.config_parameter"].sudo().get_param(
            "fulfillment.shipping_service_policy_map"
        )
        if not raw:
            return policy
        try:
            mapping = json.loads(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "fulfillment.shipping_service_policy_map is invalid JSON; ignoring"
            )
            return policy
        if not isinstance(mapping, dict):
            return policy

        configured = None
        for key, value in mapping.items():
            if self._normalize_shipping_text(str(key)) in context.get(
                "normalized_options", ()
            ):
                configured = value
                policy["matched_configuration"] = True
                policy["requires_mapping"] = False
                break
        if isinstance(configured, str):
            policy["allowed_service_ids"] = [configured]
        elif isinstance(configured, list):
            policy["allowed_service_ids"] = [
                str(value) for value in configured if value
            ]
        elif isinstance(configured, dict):
            services = configured.get("service_ids")
            carriers = configured.get("carrier_ids")
            if isinstance(services, list):
                policy["allowed_service_ids"] = [str(value) for value in services if value]
            if isinstance(carriers, list):
                policy["allowed_carrier_ids"] = [str(value) for value in carriers if value]
            if configured.get("max_estimated_days") is not None:
                policy["max_estimated_days"] = configured.get("max_estimated_days")
        return policy

    def _select_shipping_rate(
        self, rates: list, *, return_details=False, audit_context=None
    ):
        """Select by stable IDs, delivery promise, and effective cost.

        The method deliberately ignores carrier/service display names.  A
        rejected or anomalously expensive candidate holds the order instead of
        silently buying postage.
        """
        self.ensure_one()
        if not rates:
            raise exceptions.UserError("No shipping rates were returned.")

        from odoo.addons.shopify_fulfillment.services.rate_policy import (
            normalize_amazon_rate,
            normalize_shippo_rate,
            select_best_rate,
        )

        normalized = []
        rate_by_id = {}
        amazon_allowed_rate_ids = []
        for rate in rates:
            rate_id = rate.get("object_id") or rate.get("id")
            if rate_id:
                rate_by_id[str(rate_id)] = rate
            if rate.get("_source") == "amazon_shipping":
                amazon_meta = rate.get("_amazon") or {}
                normalized.append(
                    normalize_amazon_rate(amazon_meta.get("raw_rate") or {})
                )
                if (
                    rate_id
                    and amazon_meta.get("document_spec")
                    and not amazon_meta.get("requires_additional_inputs")
                    and not amazon_meta.get("unresolved_required_vas_groups")
                ):
                    amazon_allowed_rate_ids.append(str(rate_id))
            else:
                normalized.append(normalize_shippo_rate(rate))

        request_policy = self._shipping_policy_for_request()
        if request_policy.get("requires_mapping"):
            request_policy["allowed_service_ids"] = []
        is_amazon_rates = any(
            rate.get("_source") == "amazon_shipping" for rate in rates
        )
        latest_delivery = self.amazon_latest_delivery_at if self.source == "amazon" else None
        if self.source == "amazon" and not latest_delivery:
            raise exceptions.UserError(
                "Amazon delivery deadline is missing. Order held before buying a Shippo label."
            )

        params = self.env["ir.config_parameter"].sudo()
        max_premium = params.get_param(
            "fulfillment.max_rate_premium_absolute", "25.00"
        )
        max_premium_percent = params.get_param(
            "fulfillment.max_rate_premium_percent", "50"
        )
        if request_policy.get("max_estimated_days") and not latest_delivery:
            # Ground rates are not a valid anomaly baseline for an explicitly
            # expedited checkout option.
            max_premium = None
            max_premium_percent = None
        selection = select_best_rate(
            normalized,
            currency=self.order_currency or "USD",
            allowed_rate_ids=amazon_allowed_rate_ids if is_amazon_rates else None,
            allowed_carrier_ids=request_policy.get("allowed_carrier_ids"),
            allowed_service_ids=request_policy.get("allowed_service_ids"),
            latest_delivery=latest_delivery,
            ship_at=fields.Datetime.now(),
            max_estimated_days=(
                None if latest_delivery else request_policy.get("max_estimated_days")
            ),
            max_over_cheapest=max_premium,
            max_over_cheapest_percent=max_premium_percent,
        )

        rejection_summary = json.dumps(
            [
                {
                    "rate_id": rejection.rate_id,
                    "service_id": rejection.service_id,
                    "carrier_id": rejection.carrier_id,
                    "reasons": list(rejection.reasons),
                }
                for rejection in selection.rejections
            ],
            sort_keys=True,
        )
        details = {
            "policy_version": "structured-rate-policy-v1",
            "reason": selection.reason,
            "cheapest_eligible_amount": (
                str(selection.candidate.amount)
                if selection.candidate and selection.candidate.amount is not None
                else "0"
            ),
            "over_cheapest": (
                str(selection.over_cheapest)
                if selection.over_cheapest is not None
                else "0"
            ),
            "rejection_summary": rejection_summary,
        }

        if not selection.approved:
            candidate = selection.candidate
            candidate_text = ""
            if candidate and candidate.amount is not None:
                candidate_text = f" Candidate was {candidate.amount} {candidate.currency}."
            if audit_context:
                candidate_rate = (
                    rate_by_id.get(str(candidate.rate_id)) if candidate else {}
                )
                try:
                    self.env["fulfillment.rate.audit"].sudo().log_purchase(
                        order=self,
                        shipment=False,
                        group=audit_context.get("group"),
                        sequence=audit_context.get("sequence") or 0,
                        weight_grams=audit_context.get("weight_grams") or 0.0,
                        rates=rates,
                        selected_rate=candidate_rate or {},
                        is_residential=audit_context.get("is_residential"),
                        rate_meta=audit_context.get("rate_meta") or {},
                        selection=details,
                        decision="held",
                    )
                except Exception:
                    _logger.exception(
                        "Order %s: Failed to record held shipping decision",
                        self.id,
                    )
            raise ShippingPolicyHold(
                "Shipping policy held this order before label purchase: "
                f"{selection.reason}.{candidate_text} Review the rate audit and order promise."
            )

        selected_rate = rate_by_id.get(str(selection.selected.rate_id))
        if not selected_rate:
            raise exceptions.UserError(
                "Shipping policy selected a rate that is no longer available. Re-rate the order."
            )

        _logger.info(
            "Order %s: Structured policy selected rate_id=%s service_id=%s "
            "carrier_id=%s amount=%s %s reason=%s",
            self.id,
            selection.selected.rate_id,
            selection.selected.service_id,
            selection.selected.carrier_id,
            selection.selected.amount,
            selection.selected.currency,
            selection.reason,
        )
        return (selected_rate, details) if return_details else selected_rate
