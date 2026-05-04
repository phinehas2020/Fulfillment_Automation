"""Restock detection records produced from POS inventory sync.

When a POS order's inventory sync sees a variant whose post-sale Shopify quantity
falls below its `custom.restock_level` metafield, we materialize a snapshot here
and create / merge an Odoo project task. When the task is marked done, we
transfer the recommended quantity from the configured warehouse to the POS
retail stock location.
"""

import logging
from typing import Any, List

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ShopifyRestockItem(models.Model):
    _name = "shopify.restock.item"
    _description = "Shopify Restock Item"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=False)

    product_title = fields.Char(required=True)
    variant_title = fields.Char()
    sku = fields.Char(index=True)
    product_handle = fields.Char()
    product_url = fields.Char(string="Product URL")

    current_qty = fields.Integer(string="Current Qty")
    restock_level = fields.Integer(string="Restock Level")
    restock_amount = fields.Integer(string="Recommended Order")

    product_id_global = fields.Char(string="Shopify Product ID")
    variant_id_global = fields.Char(string="Shopify Variant ID")
    shopify_location_id = fields.Char(string="Shopify Location ID", index=True)
    identity_key = fields.Char(index=True, copy=False)
    is_active_snapshot = fields.Boolean(
        string="Active Snapshot",
        default=True,
        index=True,
        copy=False,
    )
    superseded_by_item_id = fields.Many2one(
        comodel_name="shopify.restock.item",
        string="Superseded By",
        ondelete="set null",
        copy=False,
    )
    superseded_at = fields.Datetime(copy=False)
    superseded_reason = fields.Char(copy=False)

    todo_task_id = fields.Many2one(
        comodel_name="project.task",
        string="To-do Task",
        ondelete="set null",
    )
    task_state = fields.Char(
        string="Task Status",
        compute="_compute_task_state",
        store=False,
    )

    source_pos_order_id = fields.Many2one(
        comodel_name="shopify.order",
        string="POS Order",
        ondelete="set null",
        help="POS order whose inventory sync produced this detection.",
    )

    inventory_move_id = fields.Many2one(
        comodel_name="stock.move",
        string="Inventory Move",
        ondelete="set null",
    )
    inventory_transferred = fields.Boolean(
        string="Inventory Transferred",
        default=False,
        copy=False,
    )
    inventory_transferred_at = fields.Datetime(copy=False)
    inventory_transferred_by = fields.Many2one(
        comodel_name="res.users",
        string="Transferred By",
        copy=False,
    )
    inventory_transfer_error = fields.Char(string="Transfer Error", copy=False)

    @api.depends("product_title", "variant_title", "restock_amount")
    def _compute_name(self):
        for item in self:
            item.name = self._build_task_title_for(item)

    @api.depends("todo_task_id", "todo_task_id.state", "todo_task_id.stage_id")
    def _compute_task_state(self):
        for item in self:
            task = item.todo_task_id
            if not task:
                item.task_state = "No Task"
            elif "state" in task._fields and task.state:
                item.task_state = task.state
            elif task.stage_id:
                item.task_state = task.stage_id.name
            else:
                item.task_state = "Unknown"

    # ---------------------------
    # Identity / titles
    # ---------------------------
    @staticmethod
    def _normalize_identity_piece(value: Any) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _compute_identity_key(
        cls,
        *,
        location_piece: Any = None,
        variant_id_global: Any = None,
        product_id_global: Any = None,
        sku: Any = None,
        product_title: Any = None,
        variant_title: Any = None,
    ) -> str:
        loc_piece = cls._normalize_identity_piece(location_piece) or "0"
        variant_piece = cls._normalize_identity_piece(variant_id_global)
        product_piece = cls._normalize_identity_piece(product_id_global)
        sku_piece = cls._normalize_identity_piece(sku)
        if variant_piece:
            identity_piece = f"variant:{variant_piece}"
        elif product_piece and sku_piece:
            identity_piece = f"product:{product_piece}|sku:{sku_piece}"
        elif product_piece:
            identity_piece = f"product:{product_piece}"
        elif sku_piece:
            identity_piece = f"sku:{sku_piece}"
        else:
            identity_piece = (
                f"title:{cls._normalize_identity_piece(product_title)}"
                f"|variant:{cls._normalize_identity_piece(variant_title)}"
            )
        return f"loc:{loc_piece}|{identity_piece}"

    @staticmethod
    def _build_task_title_for(item) -> str:
        display_title = item.product_title or "Restock Item"
        if item.variant_title and item.variant_title != "Default Title":
            display_title += f" - {item.variant_title}"
        qty = max(int(item.restock_amount or 0), 0)
        return f"{display_title} | {qty}"

    # ---------------------------
    # Project / task helpers
    # ---------------------------
    @api.model
    def _get_restock_project(self, create_if_missing: bool = True):
        ICP = self.env["ir.config_parameter"].sudo()
        project_id_raw = ICP.get_param("fulfillment.restock_project_id")
        if project_id_raw and str(project_id_raw).isdigit():
            project = self.env["project.project"].sudo().browse(int(project_id_raw))
            if project.exists():
                return project
        project = self.env["project.project"].sudo().search(
            [("name", "=", "Shopify Restock")], limit=1
        )
        if project or not create_if_missing:
            return project
        return self.env["project.project"].sudo().create(
            {"name": "Shopify Restock", "company_id": self.env.company.id}
        )

    def _description_lines(self) -> List[str]:
        self.ensure_one()
        lines = [
            f"Product: {self.product_title or ''}",
            f"Variant: {self.variant_title or ''}",
            f"SKU: {self.sku or ''}",
            f"Current Qty: {self.current_qty or 0}",
            f"Restock Level: {self.restock_level or ''}",
            f"Recommended Order: {self.restock_amount or 0}",
        ]
        if self.product_url:
            lines.append(f"Shopify URL: {self.product_url}")
        if self.source_pos_order_id:
            lines.append(f"Triggered by POS order: {self.source_pos_order_id.order_name or ''}")
        return [line for line in lines if line]

    def _find_existing_open_task(self, project):
        self.ensure_one()
        if not project or not self.identity_key:
            return self.env["project.task"]
        candidates = self.sudo().search([
            ("identity_key", "=", self.identity_key),
            ("todo_task_id", "!=", False),
        ], order="id desc")
        for candidate in candidates:
            task = candidate.todo_task_id
            if not task or task.project_id.id != project.id:
                continue
            if "state" in task._fields and task.state == "1_canceled":
                continue
            if task._restock_task_is_done():
                continue
            return task
        return self.env["project.task"]

    def _supersede_active_snapshots_for_task(self, task):
        self.ensure_one()
        if not task or not self.identity_key:
            return
        domain = [
            ("todo_task_id", "=", task.id),
            ("identity_key", "=", self.identity_key),
            ("is_active_snapshot", "=", True),
            ("inventory_transferred", "=", False),
            ("id", "!=", self.id),
        ]
        siblings = self.sudo().search(domain)
        if siblings:
            siblings.write({
                "is_active_snapshot": False,
                "superseded_by_item_id": self.id,
                "superseded_at": fields.Datetime.now(),
                "superseded_reason": "replaced_by_new_pos_run",
            })

    def _create_or_merge_task(self):
        """Create a new task or merge into an existing open one. Returns the task."""
        self.ensure_one()
        project = self._get_restock_project(create_if_missing=True)
        existing = self._find_existing_open_task(project)
        if existing:
            self._supersede_active_snapshots_for_task(existing)
            self.write({
                "todo_task_id": existing.id,
                "is_active_snapshot": True,
                "superseded_by_item_id": False,
                "superseded_at": False,
                "superseded_reason": False,
            })
            existing.sudo().write({
                "restock_item_id": self.id,
                "name": self._build_task_title_for(self),
                "description": "\n".join(self._description_lines()),
            })
            return existing

        task_vals = {
            "name": self._build_task_title_for(self),
            "description": "\n".join(self._description_lines()),
            "project_id": project.id if project else False,
            "restock_item_id": self.id,
        }
        task_model = self.env["project.task"]
        if "user_id" not in task_model._fields and "user_ids" in task_model._fields:
            user_id_raw = self.env["ir.config_parameter"].sudo().get_param(
                "fulfillment.default_user_id"
            )
            if user_id_raw and str(user_id_raw).isdigit():
                task_vals["user_ids"] = [(6, 0, [int(user_id_raw)])]
        task = task_model.with_context(
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
            mail_auto_subscribe_no_notify=True,
            mail_notify_force_send=False,
            tracking_disable=True,
        ).sudo().create(task_vals)
        self.write({"todo_task_id": task.id})
        return task

    # ---------------------------
    # Inventory transfer on done
    # ---------------------------
    def _get_odoo_product(self):
        self.ensure_one()
        if not self.sku:
            return self.env["product.product"].sudo()
        return self.env["product.product"].sudo().search(
            [("default_code", "=", self.sku)], limit=1
        )

    def _get_source_location(self):
        """Source warehouse: dedicated restock setting, fall back to fulfillment source."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        for key in (
            "fulfillment.restock_source_location_id",
            "fulfillment.stock_location_id",
        ):
            raw = ICP.get_param(key)
            if not raw:
                continue
            try:
                location_id = int(raw)
            except (TypeError, ValueError):
                continue
            if location_id <= 0:
                continue
            location = self.env["stock.location"].sudo().browse(location_id)
            if location.exists():
                return location
        try:
            return self.env.ref("stock.stock_location_stock")
        except Exception:  # pylint: disable=broad-except
            return self.env["stock.location"].sudo().search(
                [("usage", "=", "internal")], limit=1
            )

    def _get_destination_location(self):
        """Destination is the configured POS retail stock location."""
        self.ensure_one()
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "fulfillment.pos_stock_location_id"
        )
        if not raw:
            return self.env["stock.location"].sudo()
        try:
            location_id = int(raw)
        except (TypeError, ValueError):
            return self.env["stock.location"].sudo()
        if location_id <= 0:
            return self.env["stock.location"].sudo()
        location = self.env["stock.location"].sudo().browse(location_id)
        return location if location.exists() else self.env["stock.location"].sudo()

    def _create_inventory_move(self, product, quantity, source_location, dest_location):
        self.ensure_one()
        move_vals = {
            "name": f"Restock Transfer: {self.product_title} ({self.sku or product.display_name})",
            "product_id": product.id,
            "product_uom_qty": quantity,
            "product_uom": product.uom_id.id,
            "location_id": source_location.id,
            "location_dest_id": dest_location.id,
            "company_id": source_location.company_id.id or self.env.company.id,
            "origin": (
                self.source_pos_order_id.order_name
                if self.source_pos_order_id else f"Restock #{self.id}"
            ),
            "reference": f"Restock: {self.product_title}",
        }
        move = self.env["stock.move"].sudo().create(move_vals)
        move._action_confirm()
        move._action_assign()
        if hasattr(move, "_set_quantity_done"):
            move._set_quantity_done(quantity)
        elif "quantity_done" in move._fields:
            move.quantity_done = quantity
        else:
            self.env["stock.move.line"].sudo().create({
                "move_id": move.id,
                "product_id": product.id,
                "product_uom_id": product.uom_id.id,
                "qty_done": quantity,
                "location_id": source_location.id,
                "location_dest_id": dest_location.id,
                "company_id": move.company_id.id,
            })
        move._action_done()
        return move

    def action_transfer_inventory(self):
        """Move recommended qty from warehouse to POS retail when task completes."""
        for item in self:
            if not item.is_active_snapshot:
                continue
            if item.inventory_transferred:
                continue
            qty = int(item.restock_amount or 0)
            if qty <= 0:
                item.sudo().write(
                    {"inventory_transfer_error": "No restock amount to transfer."}
                )
                continue
            product = item._get_odoo_product()
            if not product:
                item.sudo().write({
                    "inventory_transfer_error":
                        f"No Odoo product found for SKU '{item.sku or ''}'.",
                })
                continue
            source_location = item._get_source_location()
            if not source_location:
                item.sudo().write({
                    "inventory_transfer_error":
                        "No source location configured. Set Restock Source Location"
                        " (or fall back to Online Fulfillment Source Location) in"
                        " Shopify Settings.",
                })
                continue
            dest_location = item._get_destination_location()
            if not dest_location:
                item.sudo().write({
                    "inventory_transfer_error":
                        "No destination configured. Set POS Retail Stock Location"
                        " in Shopify Settings.",
                })
                continue
            try:
                move = item._create_inventory_move(
                    product, qty, source_location, dest_location
                )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception(
                    "Restock inventory transfer failed for item %s", item.id
                )
                item.sudo().write({
                    "inventory_transfer_error": f"Transfer failed: {str(exc)[:200]}",
                })
                continue
            transferred_at = fields.Datetime.now()
            item.sudo().write({
                "inventory_move_id": move.id,
                "inventory_transferred": True,
                "inventory_transferred_at": transferred_at,
                "inventory_transferred_by":
                    item.env.context.get("transferred_by_uid") or item.env.user.id,
                "inventory_transfer_error": False,
                "is_active_snapshot": False,
                "superseded_at": transferred_at,
                "superseded_reason": "transferred",
            })
            _logger.info(
                "Restock transfer complete: %s units of %s -> %s (item %s, move %s)",
                qty, product.display_name, dest_location.display_name, item.id, move.id,
            )
