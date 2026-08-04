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
from odoo.tools.float_utils import float_compare

from ..services.shopify_api import ShopifyAPI

_logger = logging.getLogger(__name__)

HISTORICAL_SHOPIFY_MISSING_PREFIX = "Historical Shopify update missing:"


class ShopifyRestockItem(models.Model):
    _name = "fulfillment.restock.item"
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
        comodel_name="fulfillment.restock.item",
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
    inventory_transfer_warning = fields.Char(string="Transfer Warning", copy=False)

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
                "fulfillment_restock_item_id": self.id,
                "name": self._build_task_title_for(self),
                "description": "\n".join(self._description_lines()),
            })
            return existing

        task_vals = {
            "name": self._build_task_title_for(self),
            "description": "\n".join(self._description_lines()),
            "project_id": project.id if project else False,
            "fulfillment_restock_item_id": self.id,
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

    def _get_shopify_source_location_id(self):
        """Return the Shopify Fulfillment location used as the restock source."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        for key in (
            "fulfillment.restock_shopify_source_location_id",
            "odoo_shopify_restock.source_location_id_numeric",
        ):
            raw = (ICP.get_param(key) or "").strip()
            if raw:
                return raw.split("/")[-1]
        return ""

    def _get_shopify_destination_location_id(self):
        """Return the Shopify retail location captured by the POS restock task."""
        self.ensure_one()
        return (self.shopify_location_id or "").strip().split("/")[-1]

    @api.model
    def _shopify_reconciliation_domain(self):
        """Records whose Odoo move finished before paired Shopify transfers existed."""
        return [
            ("inventory_transferred", "=", True),
            ("inventory_move_id", "!=", False),
            (
                "inventory_transfer_error",
                "=like",
                f"{HISTORICAL_SHOPIFY_MISSING_PREFIX}%",
            ),
        ]

    def _needs_shopify_reconciliation(self):
        self.ensure_one()
        return bool(
            self.inventory_transferred
            and self.inventory_move_id
            and (self.inventory_transfer_error or "").startswith(
                HISTORICAL_SHOPIFY_MISSING_PREFIX
            )
        )

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
        # Odoo 18 only completes stock moves that are explicitly picked.
        # Without this flag _action_done() silently leaves the move assigned,
        # while the restock item is incorrectly marked transferred.
        if "picked" in move._fields:
            move.picked = True
            move.move_line_ids.picked = True
        move._action_done()
        move.invalidate_recordset(["state"])
        if move.state != "done":
            raise RuntimeError(
                f"Stock move {move.id} did not complete (state: {move.state})."
            )
        return move

    def _transfer_quantity_in_shopify(
        self,
        quantity,
        reference_uri=None,
        expected_destination_after=None,
    ):
        """Move the same quantity from Shopify Fulfillment to Shopify Retail."""
        self.ensure_one()
        source_location_id = self._get_shopify_source_location_id()
        destination_location_id = self._get_shopify_destination_location_id()
        variant_id = (self.variant_id_global or "").strip()
        missing = [
            label
            for label, value in (
                ("Shopify source location", source_location_id),
                ("Shopify destination location", destination_location_id),
                ("Shopify variant", variant_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Cannot update Shopify; missing " + ", ".join(missing) + "."
            )

        reference_uri = reference_uri or (
            f"gid://homestead-gristmill/FulfillmentRestockItem/{self.id}"
        )
        return ShopifyAPI.from_env(self.env).transfer_available_inventory(
            variant_id=variant_id,
            quantity=quantity,
            source_location_id=source_location_id,
            destination_location_id=destination_location_id,
            reference_uri=reference_uri,
            expected_destination_after=expected_destination_after,
        )

    def _build_negative_inventory_warning(self, quantity, balances):
        """Describe any negative balance left by an otherwise valid transfer."""
        self.ensure_one()
        negative_balances = []
        for label, before, after, rounding in balances:
            if float_compare(after, 0, precision_rounding=rounding) < 0:
                negative_balances.append(f"{label} {before:g} -> {after:g}")
        if not negative_balances:
            return False
        product_label = f"[{self.sku}] " if self.sku else ""
        product_label += self.product_title or self.display_name
        return (
            f"Transfer completed with negative inventory for {quantity:g} units of "
            f"{product_label}: {'; '.join(negative_balances)}. "
            "Human inventory reconciliation is required."
        )

    def _notify_transfer_warning(self, warning):
        """Persist the warning in task chatter and notify assigned task users."""
        self.ensure_one()
        if not warning or not self.todo_task_id:
            return
        task = self.todo_task_id.sudo()
        try:
            partner_ids = (
                task.user_ids.mapped("partner_id").ids
                if "user_ids" in task._fields
                else []
            )
            task.message_post(body=warning, partner_ids=partner_ids)
        except Exception:  # pylint: disable=broad-except
            _logger.exception(
                "Restock item %s transferred with a warning, but its task "
                "notification failed",
                self.id,
            )

    def _reconcile_missing_shopify_transfer(self):
        """Apply Shopify only when the linked Odoo move is already safely done."""
        self.ensure_one()
        qty = int(self.restock_amount or 0)
        if qty <= 0:
            raise RuntimeError("No restock amount to reconcile.")

        product = self._get_odoo_product()
        if not product:
            raise RuntimeError(
                f"No Odoo product found for SKU '{self.sku or ''}'."
            )
        source_location = self._get_source_location()
        destination_location = self._get_destination_location()
        if not source_location or not destination_location:
            raise RuntimeError(
                "Cannot reconcile Shopify because the Odoo transfer route is not configured."
            )

        move = self.inventory_move_id
        move.invalidate_recordset(
            ["state", "product_id", "product_uom_qty", "location_id", "location_dest_id"]
        )
        if move.state != "done":
            raise RuntimeError(
                f"Existing Odoo move {move.id} is {move.state}, not done."
            )
        if move.product_id != product:
            raise RuntimeError(
                f"Existing Odoo move {move.id} is for {move.product_id.display_name}, "
                f"not {product.display_name}."
            )
        if float_compare(
            move.product_uom_qty,
            qty,
            precision_rounding=move.product_uom.rounding,
        ):
            raise RuntimeError(
                f"Existing Odoo move {move.id} moved {move.product_uom_qty:g}, "
                f"not {qty}."
            )
        if (
            move.location_id != source_location
            or move.location_dest_id != destination_location
        ):
            raise RuntimeError(
                f"Existing Odoo move {move.id} used "
                f"{move.location_id.complete_name} -> "
                f"{move.location_dest_id.complete_name}, not "
                f"{source_location.complete_name} -> "
                f"{destination_location.complete_name}."
            )

        rounding = product.uom_id.rounding
        odoo_destination_qty = self.env["stock.quant"].sudo()._get_available_quantity(
            product, destination_location
        )
        shopify_api = ShopifyAPI.from_env(self.env)
        inventory_item_id = shopify_api.get_variant_inventory_item_id(
            self.variant_id_global
        )
        shopify_destination_qty = shopify_api.get_available_inventory_quantity(
            inventory_item_id, self._get_shopify_destination_location_id()
        )
        if not float_compare(
            odoo_destination_qty,
            shopify_destination_qty,
            precision_rounding=rounding,
        ):
            self.sudo().write({
                "inventory_transfer_error": False,
                "inventory_transfer_warning": False,
                "inventory_transferred_by":
                    self.env.context.get("transferred_by_uid") or self.env.user.id,
            })
            message = (
                "Shopify inventory reconciliation required no adjustment: "
                f"Odoo Retail and Shopify Retail already both have "
                f"{odoo_destination_qty:g} units of [{self.sku or ''}] "
                f"{self.product_title or ''}."
            )
            if self.todo_task_id:
                try:
                    self.todo_task_id.sudo().message_post(body=message)
                except Exception:  # pylint: disable=broad-except
                    _logger.exception(
                        "Restock item %s matched Shopify, but its task note failed",
                        self.id,
                    )
            _logger.info(
                "Restock Shopify reconciliation skipped for item %s: "
                "Odoo Retail and Shopify Retail already match at %s",
                self.id,
                odoo_destination_qty,
            )
            return {
                "skipped": True,
                "destination_before": int(shopify_destination_qty),
                "destination_after": int(shopify_destination_qty),
            }

        expected_destination_after = int(round(odoo_destination_qty))
        if float_compare(
            odoo_destination_qty,
            expected_destination_after,
            precision_rounding=rounding,
        ):
            raise RuntimeError(
                f"Odoo Retail quantity {odoo_destination_qty:g} is not a whole unit."
            )
        projected_destination = int(shopify_destination_qty) + qty
        if projected_destination != expected_destination_after:
            raise RuntimeError(
                f"Shopify Retail would become {projected_destination} after adding "
                f"{qty}, but Odoo Retail currently has "
                f"{expected_destination_after}. No adjustment was sent."
            )

        reference_uri = (
            "gid://homestead-gristmill/"
            f"FulfillmentRestockReconciliation/{self.id}"
        )
        shopify_transfer = self._transfer_quantity_in_shopify(
            qty,
            reference_uri=reference_uri,
            expected_destination_after=expected_destination_after,
        )
        warning = self._build_negative_inventory_warning(
            qty,
            [
                (
                    "Shopify Fulfillment",
                    shopify_transfer["source_before"],
                    shopify_transfer["source_after"],
                    1,
                ),
                (
                    "Shopify Retail",
                    shopify_transfer["destination_before"],
                    shopify_transfer["destination_after"],
                    1,
                ),
            ],
        )
        self.sudo().write({
            "inventory_transfer_error": False,
            "inventory_transfer_warning": warning,
            "inventory_transferred_by":
                self.env.context.get("transferred_by_uid") or self.env.user.id,
        })
        self._notify_transfer_warning(warning)
        if self.todo_task_id:
            try:
                self.todo_task_id.sudo().message_post(
                    body=(
                        f"Shopify inventory reconciled without another Odoo move: "
                        f"{qty} units of [{self.sku or ''}] "
                        f"{self.product_title or ''}; Fulfillment "
                        f"{shopify_transfer['source_before']} -> "
                        f"{shopify_transfer['source_after']}, Retail "
                        f"{shopify_transfer['destination_before']} -> "
                        f"{shopify_transfer['destination_after']}."
                    )
                )
            except Exception:  # pylint: disable=broad-except
                _logger.exception(
                    "Restock item %s reconciled in Shopify, but its task note failed",
                    self.id,
                )
        _logger.info(
            "Restock Shopify reconciliation complete: %s units of %s "
            "(item %s, existing Odoo move %s); Shopify Fulfillment %s -> %s, "
            "Retail %s -> %s",
            qty,
            product.display_name,
            self.id,
            move.id,
            shopify_transfer["source_before"],
            shopify_transfer["source_after"],
            shopify_transfer["destination_before"],
            shopify_transfer["destination_after"],
        )
        return shopify_transfer

    def action_transfer_inventory(self):
        """Move recommended qty from warehouse to POS retail when task completes."""
        for item in self:
            if item._needs_shopify_reconciliation():
                try:
                    item._reconcile_missing_shopify_transfer()
                except Exception as exc:  # pylint: disable=broad-except
                    _logger.exception(
                        "Restock Shopify reconciliation failed for item %s", item.id
                    )
                    item.sudo().write({
                        "inventory_transfer_error": (
                            f"{HISTORICAL_SHOPIFY_MISSING_PREFIX} "
                            f"Retry failed: {str(exc)[:150]}"
                        ),
                    })
                continue
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
            if source_location == dest_location:
                item.sudo().write({
                    "inventory_transfer_error":
                        "Odoo source and destination locations must be different.",
                })
                _logger.error(
                    "Odoo source and destination are identical for restock item %s",
                    item.id,
                )
                continue
            quant_model = self.env["stock.quant"].sudo()
            odoo_source_before = quant_model._get_available_quantity(
                product, source_location, allow_negative=True
            )
            odoo_destination_before = quant_model._get_available_quantity(
                product, dest_location, allow_negative=True
            )
            if float_compare(
                odoo_source_before,
                qty,
                precision_rounding=product.uom_id.rounding,
            ) < 0:
                _logger.warning(
                    "Proceeding with restock item %s despite insufficient Odoo "
                    "source inventory: %s available, %s required",
                    item.id,
                    odoo_source_before,
                    qty,
                )
            try:
                with self.env.cr.savepoint():
                    move = item._create_inventory_move(
                        product, qty, source_location, dest_location
                    )
                    shopify_transfer = item._transfer_quantity_in_shopify(qty)
                    quant_model.invalidate_model(["quantity", "reserved_quantity"])
                    odoo_source_after = quant_model._get_available_quantity(
                        product, source_location, allow_negative=True
                    )
                    odoo_destination_after = quant_model._get_available_quantity(
                        product, dest_location, allow_negative=True
                    )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception(
                    "Restock inventory transfer failed for item %s", item.id
                )
                item.sudo().write({
                    "inventory_transfer_error": f"Transfer failed: {str(exc)[:200]}",
                })
                continue
            warning = item._build_negative_inventory_warning(
                qty,
                [
                    (
                        f"Odoo {source_location.complete_name}",
                        odoo_source_before,
                        odoo_source_after,
                        product.uom_id.rounding,
                    ),
                    (
                        f"Odoo {dest_location.complete_name}",
                        odoo_destination_before,
                        odoo_destination_after,
                        product.uom_id.rounding,
                    ),
                    (
                        "Shopify Fulfillment",
                        shopify_transfer["source_before"],
                        shopify_transfer["source_after"],
                        1,
                    ),
                    (
                        "Shopify Retail",
                        shopify_transfer["destination_before"],
                        shopify_transfer["destination_after"],
                        1,
                    ),
                ],
            )
            transferred_at = fields.Datetime.now()
            item.sudo().write({
                "inventory_move_id": move.id,
                "inventory_transferred": True,
                "inventory_transferred_at": transferred_at,
                "inventory_transferred_by":
                    item.env.context.get("transferred_by_uid") or item.env.user.id,
                "inventory_transfer_error": False,
                "inventory_transfer_warning": warning,
                "is_active_snapshot": False,
                "superseded_at": transferred_at,
                "superseded_reason": "transferred",
            })
            item._notify_transfer_warning(warning)
            _logger.info(
                "Restock transfer complete: %s units of %s, Odoo %s -> %s "
                "(item %s, move %s); Shopify Fulfillment %s -> %s, "
                "Retail %s -> %s",
                qty,
                product.display_name,
                source_location.complete_name,
                dest_location.complete_name,
                item.id,
                move.id,
                shopify_transfer["source_before"],
                shopify_transfer["source_after"],
                shopify_transfer["destination_before"],
                shopify_transfer["destination_after"],
            )
