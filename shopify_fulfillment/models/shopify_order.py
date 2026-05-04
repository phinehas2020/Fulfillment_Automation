import json
import logging
import re
import unicodedata
from html import escape
from typing import Optional

from odoo import api, exceptions, fields, models
from ..services.address_utils import normalize_address_lines

_logger = logging.getLogger(__name__)


SHIPPING_METHOD_STOP_WORDS = {
    "air",
    "delivery",
    "mail",
    "shipping",
    "service",
}


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
            ("inventory_synced", "Inventory Synced"),
            ("error", "Error"),
            ("manual_required", "Manual Review"),
        ],
        default="pending",
    )
    error_message = fields.Text()
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
        if source_name == "amazon" or "amazon" in tags:
            return "amazon"
        return "shopify"

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
        if order_reference:
            parts.append(f"<p><strong>Order:</strong> {escape(str(order_reference))}</p>")

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
            message = "POS inventory sync blocked. No Odoo stock was changed:\n"
            message += "\n".join(f"- {error}" for error in preflight_errors)
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
            message = "POS inventory sync blocked. No Odoo stock was changed:\n"
            message += "\n".join(f"- {error}" for error in preflight_errors)
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
        try:
            self._create_restock_detections_from_rows(sync_rows, shopify_location_id)
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "Restock detection failed for order %s; POS sync itself succeeded",
                self.order_name,
            )
        _logger.info("POS inventory sync completed for order %s", self.order_name)
        return True

    def _create_restock_detections_from_rows(self, sync_rows, shopify_location_id):
        """Flag below-threshold variants from a successful POS sync and (re)open tasks."""
        self.ensure_one()
        if not sync_rows:
            return
        item_model = self.env["shopify.restock.item"].sudo()
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
        pos_synced_count = 0
        skipped_count = 0
        error_count = 0
        
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
                
                # Check if auto-processing is enabled
                ICP = self.env["ir.config_parameter"].sudo()
                auto_process = ICP.get_param("fulfillment.auto_process", "False")
                if auto_process.lower() in ("true", "1", "yes"):
                    order.process_order()
                    _logger.info("Order %s auto-processed", order.id)
                    
            except Exception as e:
                _logger.exception("Failed to import order %s: %s", shopify_id, e)
                error_count += 1
        
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
        source = self._source_from_payload(payload)
        
        shipping_lines = payload.get("shipping_lines") or []
        requested_method = False
        if shipping_lines:
            requested_method = (
                shipping_lines[0].get("title")
                or shipping_lines[0].get("code")
                or shipping_lines[0].get("carrier_identifier")
            )
        
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
            "shopify_location_id": self._shopify_location_id_from_payload(payload),
        }

    def action_process(self):
        for order in self:
            order.process_order()

    def action_reset_and_reprocess(self):
        """Reset fulfillment artifacts and re-run processing."""
        self._reset_fulfillment_state()
        self.process_order()

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

        shippo_meta = {"is_residential": None, "validation_results": None}

        if shippo:
            # Get rates for this specific box with its weight
            rates, shippo_meta = shippo.get_rates_for_box(
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
            
            # Carrier fallback: If USPS fails with address validation, try UPS
            if shipment_vals and shipment_vals.get("error"):
                error_codes = shipment_vals.get("error_codes", [])
                failed_carrier = shipment_vals.get("failed_carrier", "")
                
                # Check if this is an address validation error from USPS
                is_address_error = "failed_address_validation" in error_codes
                is_usps = failed_carrier.upper() == "USPS" or "USPS" in selected_rate.get("provider", "")
                
                if is_address_error and is_usps:
                    _logger.warning(
                        "Order %s Box %d: USPS address validation failed, attempting UPS fallback",
                        self.id, sequence
                    )
                    
                    # Find a UPS rate as fallback
                    ups_rates = [
                        r for r in rates 
                        if r.get("provider", "").upper() == "UPS"
                    ]
                    
                    if ups_rates:
                        # Re-run selector so expedited requests cannot downgrade on carrier fallback.
                        ups_rate = self._select_shipping_rate(ups_rates)
                        _logger.info(
                            "Order %s Box %d: Trying UPS %s at $%s",
                            self.id, sequence,
                            ups_rate.get("servicelevel", {}).get("name"),
                            ups_rate.get("amount")
                        )
                        shipment_vals = shippo.purchase_label(ups_rate)

                        if shipment_vals and not shipment_vals.get("error"):
                            selected_rate = ups_rate
                            _logger.info(
                                "Order %s Box %d: UPS fallback successful!",
                                self.id, sequence
                            )
                    else:
                        _logger.warning(
                            "Order %s Box %d: No UPS rates available for fallback",
                            self.id, sequence
                        )
        else:
            # Fallback to Mock API (for testing)
            api_client = self._get_shopify_api()
            rates = api_client.get_shipping_rates(self)
            if not rates:
                raise exceptions.UserError(f"Box {sequence}: Mock API returned no rates")
            cheapest = sorted(rates, key=lambda r: r.get("amount", 0))[0]
            selected_rate = cheapest
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
                is_residential=shippo_meta.get("is_residential") if shippo else None,
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
        if re.search(r"\b(ground|standard|economy|saver|surepost|smartpost)\b", normalized_value):
            return "ground"
        return None

    @staticmethod
    def _shipping_provider_hint(normalized_value: str) -> Optional[str]:
        if "ups" in normalized_value:
            return "UPS"
        if "usps" in normalized_value or "postal service" in normalized_value:
            return "USPS"
        if "fedex" in normalized_value or "federal express" in normalized_value:
            return "FEDEX"
        if "dhl" in normalized_value:
            return "DHL"
        if "ontrac" in normalized_value:
            return "ONTRAC"
        return None

    @classmethod
    def _is_expedited_request(cls, normalized_value: str, speed_class: Optional[str]) -> bool:
        if speed_class in {"overnight", "two_day", "three_day", "expedited"}:
            return True
        if re.search(r"\b(next|overnight|express|expedited|priority|rush)\b", normalized_value):
            return True
        return False

    @staticmethod
    def _is_speed_compatible(requested_speed: Optional[str], candidate_speed: Optional[str]) -> bool:
        if not requested_speed or not candidate_speed:
            return False
        if requested_speed == "overnight":
            return candidate_speed == "overnight"
        if requested_speed == "two_day":
            return candidate_speed in {"two_day", "overnight"}
        if requested_speed == "three_day":
            return candidate_speed in {"three_day", "two_day", "overnight"}
        if requested_speed == "expedited":
            return candidate_speed in {"expedited", "three_day", "two_day", "overnight"}
        if requested_speed == "ground":
            return candidate_speed == "ground"
        return requested_speed == candidate_speed

    @staticmethod
    def _token_overlap_score(left: str, right: str) -> int:
        left_tokens = {
            tok for tok in left.split()
            if tok and tok not in SHIPPING_METHOD_STOP_WORDS
        }
        right_tokens = {
            tok for tok in right.split()
            if tok and tok not in SHIPPING_METHOD_STOP_WORDS
        }
        if not left_tokens or not right_tokens:
            return 0
        return len(left_tokens.intersection(right_tokens))

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

        merged = " ".join(s for s in snippets if s).strip()
        normalized = self._normalize_shipping_text(merged)
        speed_class = self._shipping_speed_class(normalized)
        return {
            "raw": merged,
            "normalized": normalized,
            "speed_class": speed_class,
            "provider_hint": self._shipping_provider_hint(normalized),
            "is_expedited": self._is_expedited_request(normalized, speed_class),
        }

    def _select_shipping_rate(self, rates: list) -> dict:
        """Select the best shipping rate from available options.

        Prefers requested_shipping_method if set, otherwise cheapest.
        """
        if not rates:
            return {}

        def _rate_amount(rate):
            try:
                return float(rate.get("amount", 999999))
            except Exception:
                return 999999.0

        # Sort by amount (cheapest first)
        cheapest = sorted(rates, key=_rate_amount)[0]
        selected_rate = cheapest

        if self.requested_shipping_method:
            req_ctx = self._requested_shipping_context()
            req_norm = req_ctx["normalized"]
            req_speed = req_ctx["speed_class"]
            req_provider = req_ctx["provider_hint"]

            enriched_rates = []
            for rate in rates:
                service = self._normalize_shipping_text(rate.get("servicelevel", {}).get("name", ""))
                provider = self._normalize_shipping_text(rate.get("provider", ""))
                combined = " ".join(v for v in (provider, service) if v).strip()
                enriched_rates.append(
                    {
                        "rate": rate,
                        "amount": _rate_amount(rate),
                        "service": service,
                        "provider": provider,
                        "combined": combined,
                        "speed_class": self._shipping_speed_class(combined),
                    }
                )

            provider_rates = []
            if req_provider:
                provider_rates = [
                    item for item in enriched_rates
                    if self._normalize_shipping_text(req_provider) in item["provider"]
                ]

            candidate_pool = provider_rates or enriched_rates

            # Highest-confidence match first: exact normalized match.
            exact_matches = [
                item for item in candidate_pool
                if item["service"] == req_norm or item["combined"] == req_norm
            ]
            if exact_matches:
                selected_rate = sorted(exact_matches, key=lambda item: item["amount"])[0]["rate"]
            else:
                # Next best: phrase containment.
                contains_matches = [
                    item for item in candidate_pool
                    if req_norm and (
                        req_norm in item["combined"]
                        or item["combined"] in req_norm
                    )
                ]
                if contains_matches:
                    selected_rate = sorted(contains_matches, key=lambda item: item["amount"])[0]["rate"]
                else:
                    # Then speed-class match (overnight, 2-day, etc).
                    speed_matches = [
                        item for item in candidate_pool
                        if self._is_speed_compatible(req_speed, item["speed_class"])
                    ]
                    if speed_matches:
                        selected_rate = sorted(speed_matches, key=lambda item: item["amount"])[0]["rate"]
                    else:
                        # Last attempt: fuzzy token overlap between requested phrase and rate name.
                        scored = [
                            (self._token_overlap_score(req_norm, item["combined"]), item)
                            for item in candidate_pool
                        ]
                        scored = [pair for pair in scored if pair[0] >= 2]
                        if scored:
                            top_score = max(pair[0] for pair in scored)
                            top_matches = [pair[1] for pair in scored if pair[0] == top_score]
                            selected_rate = sorted(top_matches, key=lambda item: item["amount"])[0]["rate"]
                        elif req_ctx["is_expedited"]:
                            available = ", ".join(
                                sorted({
                                    r.get("servicelevel", {}).get("name") or "Unknown service"
                                    for r in rates
                                })
                            )
                            raise exceptions.UserError(
                                "Requested shipping method '%s' could not be matched to an expedited Shippo "
                                "rate. Available rates: %s. Order held to prevent a slower label purchase."
                                % (self.requested_shipping_method, available)
                            )
                        else:
                            _logger.warning(
                                "Order %s: Requested shipping '%s' not found. Using cheapest.",
                                self.id,
                                self.requested_shipping_method,
                            )

            if selected_rate:
                _logger.info(
                    "Order %s: Selected rate for '%s': %s (%s) - $%s",
                    self.id,
                    self.requested_shipping_method,
                    selected_rate.get("servicelevel", {}).get("name"),
                    selected_rate.get("provider"),
                    selected_rate.get("amount"),
                )

        return selected_rate
