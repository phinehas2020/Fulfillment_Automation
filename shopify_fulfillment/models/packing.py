"""Canonical packing identity, claims, receipts and completion policy.

Public actions authorize the real actor before any bounded internal elevation.
Every mutation serializes on the same order row. Packing records progress;
stock/accounting remain in the existing task finalizer, and outbound Shopify
work is a separately committed intent processed by cron.
"""
import json
import logging
import uuid
from collections import Counter

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from ..services.packing_utils import (
    PACKING_MOBILE_REQUEST, PackingError, canonical_barcode, event_identity, fulfillment_matches,
    payload_digest, quantity_map, saved_label_barcodes, validate_allocations,
)

_logger = logging.getLogger(__name__)
POLICY = "packing_v1"
EMPLOYEE = "shopify_fulfillment.group_packing_employee"
SUPERVISOR = "shopify_fulfillment.group_packing_supervisor"


class PackingUser(models.Model):
    _inherit = "res.users"

    packing_warehouse_ids = fields.Many2many(
        "stock.warehouse", "packing_user_warehouse_rel", "user_id", "warehouse_id",
        string="Packing Warehouses", groups="base.group_system",
    )


class PackingReceipt(models.Model):
    _name = "fulfillment.packing.receipt"
    _description = "Immutable Packing Action Receipt"
    _order = "id desc"

    event_id = fields.Char(required=True, index=True, readonly=True)
    device_id = fields.Char(required=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    order_id = fields.Many2one("shopify.order", ondelete="set null", readonly=True)
    shipment_id = fields.Many2one("fulfillment.shipment", ondelete="set null", readonly=True)
    action = fields.Char(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    response_json = fields.Text(required=True, readonly=True)
    _sql_constraints = [("event_unique", "unique(event_id)", "Packing event already exists.")]

    def write(self, vals):
        raise AccessError(_("Packing action receipts are immutable."))

    def unlink(self):
        raise AccessError(_("Packing action receipts are retained for reconciliation."))


class PackingLabelIdentity(models.Model):
    _name = "fulfillment.packing.label"
    _description = "Packing Label Identity and Void Tombstone"

    barcode = fields.Char(required=True, index=True, readonly=True)
    carrier = fields.Char(readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    order_id = fields.Many2one("shopify.order", ondelete="set null", readonly=True)
    shipment_id = fields.Many2one("fulfillment.shipment", ondelete="set null", readonly=True)
    state = fields.Selection([("active", "Active"), ("void", "Voided")], default="active", required=True, readonly=True, index=True)
    void_reason = fields.Char(readonly=True)
    voided_at = fields.Datetime(readonly=True)
    _sql_constraints = [("shipment_barcode_unique", "unique(shipment_id, barcode)", "Label identity already exists.")]

    def write(self, vals):
        if not self.env.su:
            raise AccessError(_("Label identities can only change through the packing workflow."))
        return super().write(vals)

    def unlink(self):
        raise AccessError(_("Voided label identities must be preserved."))


class PackingOrder(models.Model):
    _inherit = "shopify.order"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    packing_warehouse_id = fields.Many2one("stock.warehouse", check_company=True, readonly=True)
    completion_policy_version = fields.Selection(
        [("legacy_print", "Historical Print Policy"), (POLICY, "Confirmed Packing v1"), ("review", "Historical Review Hold")],
        default="legacy_print", required=True, readonly=True, copy=False, index=True,
    )
    packing_version = fields.Integer(default=1, required=True, readonly=True, copy=False)
    packing_owner_id = fields.Many2one("res.users", readonly=True, copy=False)
    packing_operational_state = fields.Selection(
        [("pending", "Pending"), ("complete", "Complete"), ("review", "Review")], default="pending", required=True, readonly=True, copy=False,
    )
    packing_review_reason = fields.Text(readonly=True, copy=False)
    packing_completed_at = fields.Datetime(readonly=True, copy=False)
    packing_reconciliation_note = fields.Text(readonly=True, copy=False)
    packing_skip_shopify = fields.Boolean(readonly=True, copy=False)
    packing_labels = fields.One2many("fulfillment.packing.label", "order_id")

    def _packing_lock(self):
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM shopify_order WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset()
        self._packing_shipments().invalidate_recordset()
        return self

    def _packing_shipments(self):
        self.ensure_one()
        shipments = self.shipment_group_id.shipment_ids if self.shipment_group_id else self.shipment_id
        return shipments.filtered(lambda shipment: shipment.packing_state != "void").sorted("sequence")

    def _packing_authorize(self, supervisor=False, read_only=False):
        self.ensure_one()
        actor = self.env.user
        if not actor.has_group(SUPERVISOR if supervisor else EMPLOYEE):
            raise PackingError("packing_forbidden", "You do not have permission to pack this order.", 403)
        if self.company_id not in self.env.companies or self.company_id not in actor.company_ids:
            raise PackingError("company_forbidden", "This order is outside your allowed company.", 403)
        warehouse = self.packing_warehouse_id
        if not warehouse and read_only and self.completion_policy_version != POLICY:
            warehouse = self._packing_default_warehouse()
        if not warehouse or warehouse.sudo().company_id != self.company_id:
            raise PackingError("warehouse_review", "A supervisor must assign this order's packing warehouse.")
        if not actor.has_group(SUPERVISOR) and warehouse not in actor.sudo().packing_warehouse_ids:
            raise PackingError("warehouse_forbidden", "This warehouse is outside your packing assignment.", 403)
        return actor

    def _packing_check_mutable(self):
        self.ensure_one()
        if self.completion_policy_version != POLICY:
            raise PackingError("historical_review", "This existing order must be reconciled by a supervisor before packing.")
        if not self.active or self.fulfillment_type != "shipping" or self.source == "pos":
            raise PackingError("order_ineligible", "This order is not an active shipping order.")
        if self.state in ("error", "manual_required", "processing"):
            raise PackingError("order_hold", self.error_message or "This order is on hold for supervisor review.")
        if self.packing_operational_state == "review":
            raise PackingError("supervisor_review", self.packing_review_reason or "This order needs supervisor review.")

    def _packing_allocations(self):
        self.ensure_one()
        expected = {line.id: line.quantity for line in self.line_ids if line.requires_shipping and line.quantity > 0}
        maps = {}
        for shipment in self._packing_shipments():
            shipment._packing_eligible_label()
            mapping = quantity_map(shipment.line_quantities)
            if set(mapping) != set(shipment.line_ids.ids):
                raise PackingError("allocation_mismatch", "This box's item list and quantity map disagree.")
            maps[shipment.id] = mapping
        validate_allocations(expected, list(maps.values()))
        return maps

    def _packing_products(self):
        self.ensure_one()
        products = {}
        for line in self.line_ids.filtered(lambda item: item.requires_shipping and item.quantity > 0):
            sku = (line.sku or "").strip()
            if not sku:
                raise PackingError("unmapped_product", "An item is missing its SKU; supervisor review is required.")
            matches = self.env["product.product"].sudo().search([
                ("default_code", "=ilike", sku), "|", ("company_id", "=", False), ("company_id", "=", self.company_id.id)
            ])
            if not matches:
                matches = self.env["product.product"].sudo().search([
                    ("product_tmpl_id.default_code", "=ilike", sku), "|", ("company_id", "=", False), ("company_id", "=", self.company_id.id)
                ])
            if len(matches) != 1 or matches.type == "service":
                raise PackingError("unmapped_product", "SKU %s does not have one valid physical product mapping." % sku)
            products[line.id] = matches
        return products

    def _packing_refresh_eligibility(self):
        """Read Shopify immediately before physical completion; no outbound writes."""
        self.ensure_one()
        adapter = self._get_shopify_api()
        rows = adapter.get_orders([self.shopify_id])
        remote = next((row for row in rows if str(row.get("id")) == self.shopify_id), None)
        if not remote:
            raise PackingError("shopify_unavailable", "The current order could not be verified with Shopify. Try again online.", 503)
        if remote.get("cancelled_at") or remote.get("financial_status") in ("refunded", "voided", "partially_refunded"):
            raise PackingError("order_changed", "Shopify cancelled or refunded this order. Supervisor review is required.")
        if remote.get("financial_status") != "paid":
            raise PackingError("payment_review", "Shopify has not confirmed this order as paid. Packing completion and payment recording are blocked.")
        old = self._payload_dict()
        def physical_lines(payload):
            return {str(line.get("id")): (int(line.get("current_quantity", line.get("quantity", 0))), str(line.get("sku") or ""), str(line.get("variant_id") or ""))
                    for line in payload.get("line_items", []) if line.get("requires_shipping", True) and int(line.get("current_quantity", line.get("quantity", 0))) > 0}
        expected = {str(line.shopify_line_id): (line.quantity, line.sku or "", line.shopify_variant_id or "")
                    for line in self.line_ids if line.requires_shipping and line.quantity > 0}
        if physical_lines(remote) != expected:
            raise PackingError("order_changed", "Shopify item quantities or products changed after these labels were purchased.")
        address_keys = ("address1", "address2", "city", "province_code", "zip", "country_code")
        old_address, address = old.get("shipping_address") or {}, remote.get("shipping_address") or {}
        if old_address and any(str(old_address.get(key) or "").strip().casefold() != str(address.get(key) or "").strip().casefold() for key in address_keys):
            raise PackingError("address_changed", "Shopify's shipping address changed after the label was purchased.")
        risk = adapter.get_risk_level(self.shopify_id)
        if risk not in ("LOW",):
            raise PackingError("risk_review", "Shopify risk must be verified as low before packing completion.")
        if self.packing_skip_shopify:
            fulfillments = adapter.get_order_fulfillments(self.shopify_id)
            for shipment in self._packing_shipments():
                matches = [item for item in fulfillments if fulfillment_matches(item, shipment.tracking_number, shipment._packing_shopify_items())]
                if len(matches) != 1 or str(matches[0].get("id")) != shipment.shopify_fulfillment_id:
                    raise PackingError("reconciliation_changed", "The previously reconciled Shopify fulfillment no longer matches this box.")
        else:
            remaining = Counter()
            for fulfillment_order in adapter._get_fulfillable_orders(self.shopify_id):
                for line in fulfillment_order.get("line_items", []):
                    remaining[str(line.get("line_item_id"))] += int(line.get("fulfillable_quantity") or 0)
            wanted = Counter({str(line.shopify_line_id): line.quantity for line in self.line_ids if line.requires_shipping and line.quantity > 0})
            if dict(remaining) != dict(wanted):
                raise PackingError("fulfillment_changed", "Shopify's remaining fulfillable quantities no longer match this order. Supervisor review is required.")
        return True

    def _packing_finish_operational(self):
        self.ensure_one()
        if self.packing_operational_state == "complete":
            if all(shipment.packing_state == "packed" for shipment in self._packing_shipments()):
                trusted = self.sudo().with_company(self.company_id).with_context(packing_internal_finalizer=True)
                try:
                    with self.env.cr.savepoint():
                        task = trusted.ensure_fulfillment_task()
                        task._verify_completed_fulfillment_effects()
                        task.write({"fulfillment_inventory_deducted": True, "state": "1_done"})
                        for shipment in trusted._packing_shipments().filtered(lambda record: record.packing_sync_state == "not_ready"):
                            shipment.write({"packing_sync_state": "synced" if self.packing_skip_shopify else "pending"})
                except Exception as exc:
                    self.invalidate_recordset()
                    reason = str(exc) if isinstance(exc, UserError) else "Previously completed delivery/accounting needs supervisor review."
                    trusted.write({"packing_operational_state": "review", "packing_review_reason": reason})
                    return False
            return True
        if any(shipment.packing_state != "packed" for shipment in self._packing_shipments()):
            return False
        # The caller already holds the lock and authorized the actual actor.
        trusted = self.sudo().with_company(self.company_id).with_context(packing_internal_finalizer=True, packing_warehouse_id=self.packing_warehouse_id.id)
        try:
            with self.env.cr.savepoint():
                trusted._packing_products()
                task = trusted.ensure_fulfillment_task()
                task._finalize_sale_delivery_accounting_strict()
                task.with_context(packing_internal_finalizer=True).write({"state": "1_done"})
                trusted.write({"packing_operational_state": "complete", "packing_review_reason": False, "packing_completed_at": fields.Datetime.now(), "state": "ready_to_ship"})
                for shipment in trusted._packing_shipments():
                    shipment.write({"packing_sync_state": "synced" if trusted.packing_skip_shopify else "pending", "packing_sync_error": False})
        except Exception as exc:
            self.invalidate_recordset()
            self._packing_shipments().invalidate_recordset()
            _logger.exception("Packing local finalization failed for order %s", self.id)
            reason = str(exc) if isinstance(exc, (UserError, PackingError)) else "Delivery or accounting could not be completed. Supervisor review is required."
            trusted.write({"packing_operational_state": "review", "packing_review_reason": reason})
            return False
        return True

    def _packing_default_warehouse(self):
        self.ensure_one()
        configured = self.env["ir.config_parameter"].sudo().get_param("fulfillment.packing_warehouse_id")
        warehouses = self.env["stock.warehouse"].sudo().search([("company_id", "=", self.company_id.id)])
        if configured and configured.isdigit():
            warehouse = warehouses.filtered(lambda candidate: candidate.id == int(configured))
            return warehouse[:1]
        return warehouses if len(warehouses) == 1 else self.env["stock.warehouse"]

    @api.model_create_multi
    def create(self, vals_list):
        enabled = self.env["ir.config_parameter"].sudo().get_param("fulfillment.packing_enabled", "False").lower() in ("true", "1", "yes")
        prepared = []
        protected = {"completion_policy_version", "packing_version", "packing_owner_id", "packing_operational_state", "packing_review_reason", "packing_completed_at", "packing_warehouse_id", "packing_skip_shopify", "packing_reconciliation_note"}
        if not self.env.su and any("default_" + name in self.env.context for name in protected):
            raise AccessError(_("Packing state defaults cannot be supplied through a create context."))
        for original in vals_list:
            if protected.intersection(original) and not self.env.su:
                raise AccessError(_("Packing state cannot be supplied when creating an order."))
            vals = dict(original)
            if enabled and vals.get("fulfillment_type", "shipping") == "shipping" and vals.get("source", "shopify") != "pos":
                vals["completion_policy_version"] = POLICY
            if vals.get("completion_policy_version") == POLICY:
                if not self.env.su and not self.env.user.has_group(SUPERVISOR):
                    raise AccessError(_("Confirmed-packing orders must be imported by the authorized fulfillment workflow."))
                company_id = vals.get("company_id") or self.env.company.id
                if not vals.get("packing_warehouse_id"):
                    configured = self.env["ir.config_parameter"].sudo().get_param("fulfillment.packing_warehouse_id")
                    warehouses = self.env["stock.warehouse"].sudo().search([("company_id", "=", company_id)])
                    if configured and configured.isdigit():
                        warehouse = warehouses.filtered(lambda item: item.id == int(configured))[:1]
                    else:
                        warehouse = warehouses if len(warehouses) == 1 else self.env["stock.warehouse"]
                    # Populate before One2many children are created so the same
                    # authorization works for normal-user supervised imports.
                    vals["packing_warehouse_id"] = warehouse.id or False
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        protected = {"completion_policy_version", "packing_version", "packing_owner_id", "packing_operational_state", "packing_review_reason", "packing_completed_at", "packing_warehouse_id", "packing_skip_shopify", "packing_reconciliation_note"}
        if protected.intersection(vals) and not self.env.su:
            raise AccessError(_("Packing state changes must use the authorized packing actions."))
        changed = {"raw_payload", "line_ids", "active", "shipment_id", "shipment_group_id", "shipping_address_line1", "shipping_address_line2", "shipping_zip", "shipping_city", "shipping_state", "shipping_country", "company_id"}.intersection(vals)
        if changed:
            for order in self.filtered(lambda record: record.completion_policy_version == POLICY):
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                order.sudo().write({"packing_version": order.packing_version + 1})
        return super().write(vals)

    def ensure_fulfillment_task(self, state=None):
        self.ensure_one()
        if self.completion_policy_version == POLICY:
            self._packing_lock()
        return super().ensure_fulfillment_task(state=state)

    def unlink(self):
        if self.filtered(lambda order: order.completion_policy_version == POLICY):
            raise UserError(_("Archive confirmed-packing orders instead of deleting their completion history and label identities."))
        return super().unlink()

    def _packing_print_finalizes(self):
        self.ensure_one()
        enabled = self.env["ir.config_parameter"].sudo().get_param("fulfillment.packing_enabled", "False").lower() in ("true", "1", "yes")
        return self.completion_policy_version == "legacy_print" and not enabled

    def action_packing_adopt(self, warehouse_id=None, reconciliation_note=None, skip_shopify=False, local_effects_confirmed=False):
        """Supervisor-only explicit migration of individually reconciled work.

        No physical packing is inferred and no local financial operation runs.
        Already-notified historical shipments require exact external matching.
        """
        for order in self:
            if not self.env.user.has_group(SUPERVISOR):
                raise PackingError("packing_forbidden", "A packing supervisor must reconcile existing orders.", 403)
            if order.company_id not in self.env.companies or order.company_id not in self.env.user.company_ids:
                raise PackingError("company_forbidden", "This order is outside your company.", 403)
            order._packing_lock()
            if order.completion_policy_version == POLICY:
                raise PackingError("already_adopted", "This order already uses confirmed packing.")
            if not reconciliation_note or not reconciliation_note.strip():
                raise PackingError("reconciliation_required", "Record the physical and system reconciliation before adopting this order.")
            warehouse = self.env["stock.warehouse"].sudo().browse(warehouse_id).exists() if warehouse_id else order._packing_default_warehouse()
            if len(warehouse) != 1 or warehouse.company_id != order.company_id:
                raise PackingError("warehouse_review", "Select one warehouse in this order's company.")
            order._packing_allocations()
            order._packing_products()
            shipments = order._packing_shipments()
            if skip_shopify:
                external = order._get_shopify_api().get_order_fulfillments(order.shopify_id)
                for shipment in shipments:
                    matches = [row for row in external if fulfillment_matches(row, shipment.tracking_number, shipment._packing_shopify_items())]
                    if len(matches) != 1 or not matches[0].get("id"):
                        raise PackingError("reconciliation_required", "An existing Shopify fulfillment could not be uniquely matched to each box.")
                    shipment.sudo().write({"shopify_fulfillment_id": str(matches[0]["id"]), "packing_sync_state": "synced"})
            elif any(shipment.shopify_fulfillment_id for shipment in shipments) or order.state == "shipped":
                raise PackingError("reconciliation_required", "Already-notified orders need verified skip-Shopify reconciliation.")
            if local_effects_confirmed:
                sale = order.sale_order_id
                deliveries = sale.picking_ids.filtered(lambda picking: picking.picking_type_code == "outgoing" and picking.state != "cancel")
                invoices = sale.invoice_ids.filtered(lambda invoice: invoice.move_type == "out_invoice" and invoice.state != "cancel")
                if not sale or not deliveries or any(picking.state != "done" for picking in deliveries) or not invoices or any(invoice.state != "posted" or invoice.payment_state not in ("paid", "in_payment") for invoice in invoices):
                    raise PackingError("reconciliation_required", "Completed delivery and paid accounting must be verified before local effects can be skipped.")
            elif order.inventory_deducted or order.sale_order_id:
                raise PackingError("reconciliation_required", "Existing local business effects require explicit supervisor reconciliation.")
            if local_effects_confirmed:
                # The policy/warehouse are still historical; verify exact stock
                # coverage against the selected warehouse inside a savepoint.
                with self.env.cr.savepoint():
                    trusted = order.sudo()
                    trusted.write({"packing_warehouse_id": warehouse.id})
                    trusted.ensure_fulfillment_task()._verify_packing_delivery_coverage(sale)
            order.sudo().write({"completion_policy_version": POLICY, "packing_warehouse_id": warehouse.id, "packing_version": order.packing_version + 1, "packing_skip_shopify": bool(skip_shopify), "packing_operational_state": "complete" if local_effects_confirmed else "pending", "packing_reconciliation_note": reconciliation_note.strip(), "packing_review_reason": False, "state": "ready_to_ship"})
            shipments._packing_prepare_identities()
            if not local_effects_confirmed:
                task = order.sudo().ensure_fulfillment_task()
                if task._is_done_state(task.state):
                    task.write({"state": "01_in_progress"})
        return True

    def action_packing_retry_finalization(self):
        for order in self:
            order._packing_authorize(supervisor=True)
            order._packing_lock()
            if order.completion_policy_version != POLICY:
                raise PackingError("historical_review", "This order has not been reconciled for packing.")
            order._packing_allocations()
            order._packing_refresh_eligibility()
            order.sudo().write({"packing_operational_state": "pending", "packing_review_reason": False, "packing_version": order.packing_version + 1})
            order._packing_finish_operational()
        return True

    def _reset_fulfillment_state(self):
        for order in self:
            if order.completion_policy_version == POLICY:
                order._packing_authorize(supervisor=True)
                order._packing_lock()
                shipments = order._packing_shipments()
                if order.packing_operational_state == "complete" or order.inventory_deducted or order.sale_order_id or any(shipment.shopify_fulfillment_id or shipment.packing_sync_state in ("pending", "submitting", "synced") for shipment in shipments):
                    raise PackingError("reset_forbidden", "This order has completed business effects. Use reconciliation or returns instead of resetting.")
                shipments._packing_void("Supervisor reset/reprocess")
                order.sudo().write({"packing_owner_id": False, "packing_operational_state": "pending", "packing_review_reason": False, "packing_version": order.packing_version + 1})
        return super()._reset_fulfillment_state()

    def _request_label_cancellations_for_shipments(self, shipments):
        self.ensure_one()
        if self.completion_policy_version == POLICY:
            self._packing_authorize(supervisor=True)
            self._packing_lock()
            if self.packing_operational_state == "complete" or self.sale_order_id or any(shipment.packing_sync_state in ("pending", "submitting", "synced") for shipment in shipments):
                raise PackingError("cancellation_forbidden", "Completed shipments require reconciliation before cancellation.")
            shipments._packing_void("Supervisor requested label cancellation")
        return super()._request_label_cancellations_for_shipments(shipments)


class PackingShipment(models.Model):
    _inherit = "fulfillment.shipment"

    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    normalized_tracking = fields.Char(compute="_compute_normalized_tracking", store=True, index=True)
    packing_state = fields.Selection([("unstarted", "Not Started"), ("in_progress", "Picking"), ("packed", "Packed"), ("void", "Voided")], default="unstarted", required=True, readonly=True, copy=False, index=True)
    packing_confirmed_json = fields.Text(default="{}", readonly=True, copy=False)
    packing_completed_at = fields.Datetime(readonly=True, copy=False)
    packing_completed_by = fields.Many2one("res.users", readonly=True, copy=False)
    packing_completion_event = fields.Char(readonly=True, copy=False)
    packing_sync_state = fields.Selection([("not_ready", "Not Ready"), ("pending", "Pending"), ("submitting", "Reconciling Submission"), ("synced", "Synced"), ("review", "Review")], default="not_ready", required=True, readonly=True, copy=False, index=True)
    packing_sync_attempts = fields.Integer(readonly=True, copy=False)
    packing_sync_error = fields.Text(readonly=True, copy=False)
    packing_sync_started_at = fields.Datetime(readonly=True, copy=False)
    packing_sync_attempt_token = fields.Char(readonly=True, copy=False)
    packing_label_ids = fields.One2many("fulfillment.packing.label", "shipment_id")

    @api.depends("tracking_number")
    def _compute_normalized_tracking(self):
        for shipment in self:
            try:
                shipment.normalized_tracking = canonical_barcode(shipment.tracking_number)
            except PackingError:
                shipment.normalized_tracking = False

    def _packing_eligible_label(self):
        self.ensure_one()
        if self.packing_state == "void" or self.refund_status != "not_requested":
            raise PackingError("voided_label", "This label was cancelled, refunded or replaced. Use the current label.")
        if self.purchase_state != "purchased" or not self.tracking_number:
            raise PackingError("label_hold", "This label purchase has not been confirmed; supervisor review is required.")
        if self not in self.order_id._packing_shipments():
            raise PackingError("obsolete_label", "This label is no longer part of the active shipment batch.")
        return True

    def _packing_prepare_identities(self):
        Label = self.env["fulfillment.packing.label"].sudo()
        for shipment in self:
            if not shipment.order_id or not shipment.tracking_number or shipment.purchase_state != "purchased":
                continue
            for barcode in saved_label_barcodes(shipment.label_zpl, shipment.tracking_number):
                if not Label.search_count([("shipment_id", "=", shipment.id), ("barcode", "=", barcode)]):
                    Label.create({"barcode": barcode, "carrier": shipment.carrier, "company_id": shipment.company_id.id, "order_id": shipment.order_id.id, "shipment_id": shipment.id, "state": "void" if shipment.packing_state == "void" else "active"})

    def _packing_void(self, reason):
        for shipment in self:
            shipment._packing_prepare_identities()
            shipment.sudo().packing_label_ids.write({"state": "void", "void_reason": reason, "voided_at": fields.Datetime.now()})
            shipment.sudo().write({"packing_state": "void"})

    @api.model
    def packing_resolve(self, barcode, symbology=None):
        if not self.env.user.has_group(EMPLOYEE):
            raise PackingError("packing_forbidden", "You do not have packing permission.", 403)
        value = canonical_barcode(barcode)
        labels = self.env["fulfillment.packing.label"].search([("barcode", "=", value), ("company_id", "in", self.env.companies.ids)])
        # A tombstone always wins over a reused carrier identity.
        if labels.filtered(lambda label: label.state == "void"):
            raise PackingError("voided_label", "This shipping label was voided or replaced.")
        shipments = labels.mapped("shipment_id") | self.search([("normalized_tracking", "=", value), ("company_id", "in", self.env.companies.ids)])
        # Standard USPS routing barcode: accept only its saved destination ZIP,
        # canonical tracking length and exact registered shipment number.
        if not shipments and value.startswith("420") and value.isdigit():
            for zip_length in (5, 9):
                tracking = value[3 + zip_length:]
                if len(tracking) not in (20, 22):
                    continue
                candidates = self.search([("normalized_tracking", "=", tracking), ("company_id", "in", self.env.companies.ids)])
                for candidate in candidates:
                    postal = "".join(char for char in (candidate.order_id.shipping_zip or "") if char.isdigit())
                    if "USPS" in (candidate.carrier or "").upper() and postal[:zip_length] == value[3:3 + zip_length] and len(postal) >= zip_length:
                        shipments |= candidate
        if not shipments:
            raise PackingError("label_not_found", "No existing purchased shipment matches this label.", 404)
        if len(shipments) != 1:
            raise PackingError("ambiguous_label", "This barcode matches multiple shipment records. Supervisor review is required.")
        shipments.order_id._packing_authorize(read_only=True)
        shipments._packing_eligible_label()
        return shipments.packing_detail()

    def packing_detail(self):
        self.ensure_one()
        if not self.exists() or not self.order_id:
            raise PackingError("shipment_not_found", "This shipment no longer exists.", 404)
        order = self.order_id
        order._packing_authorize(read_only=True)
        mapping, products, reason = {}, {}, order.packing_review_reason or ""
        try:
            self._packing_eligible_label()
            mapping = order._packing_allocations()[self.id]
            products = order._packing_products()
        except PackingError as exc:
            reason = exc.message
        try:
            confirmed = quantity_map(self.packing_confirmed_json, allow_zero=True) if self.packing_confirmed_json != "{}" else {}
        except PackingError:
            confirmed, reason = {}, "Saved progress needs supervisor review."
        if order.completion_policy_version != POLICY and not reason:
            reason = "This existing order is read-only until a supervisor reconciles its packing and fulfillment history."
        owner = order.packing_owner_id
        editable = order.completion_policy_version == POLICY and order.active and order.fulfillment_type == "shipping" and order.source != "pos" and order.state not in ("manual_required", "error", "processing") and order.packing_operational_state != "review" and self.packing_state not in ("packed", "void") and not reason
        owned = not owner or owner == self.env.user
        items = []
        for line in self.line_ids.sorted("id"):
            product = products.get(line.id)
            required = mapping.get(line.id, 0)
            picked = confirmed.get(line.id, 0)
            items.append({"line_id": line.id, "title": line.title or (product.name if product else "Item"), "variant_title": line.variant_title or "", "sku": line.sku or "", "barcode": product.barcode or "" if product else "", "product_id": product.id if product else None, "required_quantity": required, "confirmed_quantity": picked, "remaining_quantity": max(0, required - picked)})
        siblings = order._packing_shipments()
        return {"shipment_id": self.id, "order_id": order.id, "order_name": order.order_name or order.order_number or "", "customer_name": order.customer_name or "", "tracking_number": self.tracking_number or "", "carrier": self.carrier or "", "box_number": self.sequence, "box_count": len(siblings), "packing_state": self.packing_state, "operational_state": order.packing_operational_state, "sync_state": self.packing_sync_state, "version": order.packing_version, "workflow_version": order.completion_policy_version, "owner": {"id": owner.id, "name": owner.name} if owner else None, "completed_at": fields.Datetime.to_string(self.packing_completed_at) if self.packing_completed_at else None, "items": items, "siblings": [{"shipment_id": shipment.id, "box_number": shipment.sequence, "packing_state": shipment.packing_state} for shipment in siblings], "allowed_actions": {"claim": editable and (owned or self.env.user.has_group(SUPERVISOR)), "edit": editable and owned, "complete": editable and owned and bool(mapping) and all(confirmed.get(line_id, 0) == quantity for line_id, quantity in mapping.items())}, "review_reason": reason or None}

    def _packing_event(self, action, payload, expected_version, event_id, device_id, operation):
        self.ensure_one()
        actor = self.order_id._packing_authorize()
        event_id, device_id = event_identity(event_id, device_id)
        authenticated_device = self.env.context.get("mobile_device_id")
        if authenticated_device:
            if self.env.context.get("_packing_mobile_request") is not PACKING_MOBILE_REQUEST:
                raise PackingError("device_mismatch", "Phone actions must come through the authenticated packing API.", 403)
            if authenticated_device != device_id:
                raise PackingError("device_mismatch", "The action device differs from your authenticated device.", 403)
        elif device_id != "odoo:%s" % actor.id:
            raise PackingError("device_mismatch", "Use an authenticated phone session or the Odoo Packing action.", 403)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise PackingError("invalid_version", "Refresh the order before changing it.", 422)
        signature = payload_digest({"action": action, "shipment_id": self.id, "version": expected_version, "payload": payload})
        # Globally serialize receipt identity before acquiring the order lock.
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("packing-event:" + event_id,))
        order = self.order_id._packing_lock()
        order._packing_authorize()
        Receipt = self.env["fulfillment.packing.receipt"].sudo()
        existing = Receipt.search([("event_id", "=", event_id)], limit=1)
        if existing:
            if existing.user_id != actor or existing.device_id != device_id or existing.shipment_id != self or existing.action != action or existing.payload_hash != signature:
                raise PackingError("event_conflict", "This event identity has already been used for a different action.")
            # Retain immutable original evidence, but report current server state.
            # A later hold/void must never be hidden by an old success receipt.
            return self.packing_detail()
        if order.packing_version != expected_version:
            raise PackingError("version_conflict", "This order changed on another device. Refresh it before continuing.")
        order._packing_check_mutable()
        self._packing_eligible_label()
        order._packing_allocations()
        operation(actor, order)
        order.sudo().write({"packing_version": order.packing_version + 1})
        result = self.packing_detail()
        Receipt.create({"event_id": event_id, "device_id": device_id, "user_id": actor.id, "company_id": order.company_id.id, "order_id": order.id, "shipment_id": self.id, "action": action, "payload_hash": signature, "response_json": json.dumps(result)})
        return result

    def packing_claim(self, expected_version, event_id, device_id, user_id=None):
        def operation(actor, order):
            target = actor
            if user_id and user_id != actor.id:
                order._packing_authorize(supervisor=True)
                target = self.env["res.users"].sudo().browse(user_id).exists()
                if len(target) != 1 or not target.active or not target.has_group(EMPLOYEE) or order.company_id not in target.company_ids or (not target.has_group(SUPERVISOR) and order.packing_warehouse_id not in target.packing_warehouse_ids):
                    raise PackingError("invalid_owner", "Select an authorized employee for this warehouse.")
            if order.packing_owner_id and order.packing_owner_id != actor and not actor.has_group(SUPERVISOR):
                raise PackingError("claim_conflict", "This order is being packed by %s." % order.packing_owner_id.name)
            order.sudo().write({"packing_owner_id": target.id})
            order.sudo().ensure_fulfillment_task().write({"user_ids": [(6, 0, [target.id])]})
            if self.packing_state == "unstarted":
                self.sudo().write({"packing_state": "in_progress"})
        return self._packing_event("claim", {"user_id": user_id}, expected_version, event_id, device_id, operation)

    def packing_set_quantity(self, line_id, quantity, expected_version, event_id, device_id):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise PackingError("invalid_quantity", "Confirm a whole, nonnegative item quantity.", 422)
        def operation(actor, order):
            if self.packing_state == "packed":
                raise PackingError("already_packed", "This box is already packed.")
            if order.packing_owner_id and order.packing_owner_id != actor:
                raise PackingError("claim_conflict", "This order is being packed by %s." % order.packing_owner_id.name)
            mapping = quantity_map(self.line_quantities)
            if isinstance(line_id, bool) or line_id not in mapping or quantity > mapping[line_id]:
                raise PackingError("quantity_out_of_range", "The confirmed quantity must be within this box's required quantity.", 422)
            order._packing_products()
            if not order.packing_owner_id:
                order.sudo().write({"packing_owner_id": actor.id})
                order.sudo().ensure_fulfillment_task().write({"user_ids": [(6, 0, [actor.id])]})
            confirmed = json.loads(self.packing_confirmed_json or "{}")
            confirmed[str(line_id)] = quantity
            self.sudo().write({"packing_confirmed_json": json.dumps(confirmed, sort_keys=True), "packing_state": "in_progress"})
        return self._packing_event("quantity", {"line_id": line_id, "quantity": quantity}, expected_version, event_id, device_id, operation)

    def packing_complete(self, barcode, symbology, expected_version, event_id, device_id):
        canonical = canonical_barcode(barcode)
        def operation(actor, order):
            if order.packing_owner_id != actor:
                raise PackingError("claim_conflict", "Claim this order before finishing its box.")
            try:
                resolved = self.packing_resolve(barcode, symbology)
            except PackingError as exc:
                if exc.code in ("label_not_found", "unsupported_barcode", "invalid_barcode"):
                    raise PackingError("label_mismatch", "Scan the same label attached to this box.") from exc
                raise
            if resolved["shipment_id"] != self.id:
                raise PackingError("label_mismatch", "Scan the same label attached to this box.")
            if self.packing_state == "packed":
                return
            mapping = quantity_map(self.line_quantities)
            confirmed = quantity_map(self.packing_confirmed_json, allow_zero=True)
            if confirmed != mapping:
                raise PackingError("incomplete_items", "Confirm every required item before scanning to finish.")
            order._packing_products()
            order._packing_refresh_eligibility()
            self.sudo().write({"packing_state": "packed", "packing_completed_at": fields.Datetime.now(), "packing_completed_by": actor.id, "packing_completion_event": event_id})
            order._packing_finish_operational()
        return self._packing_event("complete", {"barcode": canonical, "symbology": symbology}, expected_version, event_id, device_id, operation)

    def _packing_shopify_items(self):
        self.ensure_one()
        mapping = quantity_map(self.line_quantities)
        if set(mapping) != set(self.line_ids.ids):
            raise PackingError("allocation_mismatch", "Shipment allocations need review before Shopify synchronization.")
        items = []
        for line in self.line_ids:
            if not line.shopify_line_id:
                raise PackingError("unmapped_shopify_line", "An item has no Shopify line identity.")
            items.append({"shopify_line_id": line.shopify_line_id, "quantity": mapping[line.id]})
        return items

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(key.startswith("default_packing_") for key in self.env.context):
            raise AccessError(_("Packing shipment defaults cannot be supplied through a create context."))
        for vals in vals_list:
            if any(key.startswith("packing_") for key in vals) and not self.env.su:
                raise AccessError(_("Packing state cannot be supplied when creating a shipment."))
            order = self.env["shopify.order"].browse(vals.get("order_id") or self.env.context.get("default_order_id")).exists() if vals.get("order_id") or self.env.context.get("default_order_id") else self.env["shopify.order"]
            if order and order.completion_policy_version == POLICY:
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete":
                    raise PackingError("completed_order", "Completed orders cannot receive additional shipment boxes.")
                order.sudo().write({"packing_version": order.packing_version + 1})
        shipments = super().create(vals_list)
        for shipment in shipments:
            if shipment.group_id and shipment.group_id.order_id != shipment.order_id:
                raise PackingError("allocation_mismatch", "A box and its group must belong to the same order.")
            if any(line.order_id != shipment.order_id for line in shipment.line_ids):
                raise PackingError("allocation_mismatch", "Box items must belong to the same order as their label.")
        shipments._packing_prepare_identities()
        return shipments

    def write(self, vals):
        protected = {key for key in vals if key.startswith("packing_")}
        if protected and not self.env.su:
            raise AccessError(_("Packing state changes must use the authorized actions."))
        if {"tracking_number", "line_quantities", "line_ids", "group_id", "order_id", "purchase_state", "refund_status", "label_zpl"}.intersection(vals):
            orders = self.mapped("order_id")
            if vals.get("order_id"):
                orders |= self.env["shopify.order"].browse(vals["order_id"])
            if vals.get("group_id"):
                orders |= self.env["fulfillment.shipment.group"].browse(vals["group_id"]).order_id
            for order in orders.filtered(lambda record: record.completion_policy_version == POLICY).sorted("id"):
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete" and {"tracking_number", "line_quantities", "line_ids", "group_id", "order_id"}.intersection(vals):
                    raise PackingError("completed_shipment", "Completed shipment identities and allocations cannot be replaced.")
                order.sudo().write({"packing_version": order.packing_version + 1})
            if "tracking_number" in vals:
                for shipment in self.filtered(lambda record: record.tracking_number and record.tracking_number != vals["tracking_number"]):
                    shipment._packing_prepare_identities()
                    shipment.sudo().packing_label_ids.write({"state": "void", "void_reason": "Tracking identity replaced", "voided_at": fields.Datetime.now()})
        result = super().write(vals)
        for shipment in self:
            if shipment.group_id and shipment.group_id.order_id != shipment.order_id:
                raise PackingError("allocation_mismatch", "A box and its shipment group must belong to the same order.")
            if any(line.order_id != shipment.order_id for line in shipment.line_ids):
                raise PackingError("allocation_mismatch", "Box items must belong to the same order as their label.")
        if {"tracking_number", "label_zpl", "purchase_state"}.intersection(vals):
            self._packing_prepare_identities()
        return result

    def unlink(self):
        for order in self.mapped("order_id").filtered(lambda record: record.completion_policy_version == POLICY):
            order._packing_authorize(supervisor=True)
            order._packing_lock()
            if order.sale_order_id or order.packing_operational_state == "complete" or any(shipment.shopify_fulfillment_id for shipment in self.filtered(lambda row: row.order_id == order)):
                raise PackingError("delete_forbidden", "Completed shipment records must be retained.")
        self._packing_void("Shipment record removed")
        return super().unlink()

    @api.model
    def cron_sync_packed_shipments(self, limit=20):
        """Commit a submission guard before Shopify; ambiguous outcomes only reconcile.

        Separate cursors ensure local stock/accounting intent has committed and
        the `submitting` guard survives a crash/timeout before any HTTP POST.
        An uncertain submission is never blindly repeated.
        """
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only the scheduled system job may synchronize packed shipments."))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise UserError(_("Synchronization batch size must be between 1 and 100."))
        self.env.cr.execute("SELECT id FROM fulfillment_shipment WHERE packing_sync_state IN ('pending', 'submitting') ORDER BY id LIMIT %s", (limit,))
        ids = [row[0] for row in self.env.cr.fetchall()]
        for shipment_id in ids:
            try:
                self._packing_sync_one(shipment_id)
            except Exception:
                _logger.exception("Packing Shopify reconciliation failed for shipment %s", shipment_id)
        return True

    @api.model
    def _packing_sync_one(self, shipment_id):
        from odoo import SUPERUSER_ID
        with self.env.registry.cursor() as cursor:
            env = api.Environment(cursor, SUPERUSER_ID, {})
            shipment = env["fulfillment.shipment"].browse(shipment_id).exists()
            if not shipment:
                return
            order = shipment.order_id._packing_lock()
            if order.completion_policy_version != POLICY or order.packing_operational_state != "complete" or shipment.packing_state != "packed" or shipment.packing_sync_state not in ("pending", "submitting"):
                return
            if shipment.packing_sync_state == "submitting" and shipment.packing_sync_started_at and (fields.Datetime.now() - shipment.packing_sync_started_at).total_seconds() < 120:
                # Another worker may still own the persisted submission lease.
                return
            items = shipment._packing_shopify_items()
            adapter = order._get_shopify_api()
            external = adapter.get_order_fulfillments(order.shopify_id)
            matches = [row for row in external if fulfillment_matches(row, shipment.tracking_number, items)]
            if len(matches) == 1 and matches[0].get("id"):
                shipment.write({"packing_sync_state": "synced", "shopify_fulfillment_id": str(matches[0]["id"]), "packing_sync_error": False})
                cursor.commit()
                return
            if matches or shipment.shopify_fulfillment_id or shipment.packing_sync_state == "submitting":
                shipment.write({"packing_sync_state": "review", "packing_sync_error": "Shopify submission outcome is uncertain. Reconcile the exact tracking and items before any resubmission."})
                cursor.commit()
                return
            attempt_token = str(uuid.uuid4())
            shipment.write({"packing_sync_state": "submitting", "packing_sync_started_at": fields.Datetime.now(), "packing_sync_attempts": shipment.packing_sync_attempts + 1, "packing_sync_attempt_token": attempt_token})
            cursor.commit()
            # The persisted guard precedes the only possible POST.
            try:
                response = adapter.create_fulfillment(order, {"tracking_number": shipment.tracking_number, "tracking_url": shipment.tracking_url, "carrier": shipment.carrier}, line_items=items)
                fulfillment = (response or {}).get("fulfillment") or {}
                if not fulfillment.get("id"):
                    raise UserError("Shopify did not confirm a fulfillment identity.")
                order._packing_lock()
                shipment.invalidate_recordset()
                if shipment.packing_sync_state == "submitting" and shipment.packing_sync_attempt_token == attempt_token:
                    shipment.write({"packing_sync_state": "synced", "shopify_fulfillment_id": str(fulfillment["id"]), "packing_sync_error": False})
            except Exception:
                _logger.exception("Packing Shopify submission requires reconciliation for shipment %s", shipment.id)
                order._packing_lock()
                shipment.invalidate_recordset()
                if shipment.packing_sync_state == "submitting" and shipment.packing_sync_attempt_token == attempt_token:
                    shipment.write({"packing_sync_error": "The Shopify response was uncertain. The next synchronization will reconcile external state before any action."})
            cursor.commit()

    def action_packing_retry_sync(self):
        for shipment in self:
            shipment.order_id._packing_authorize(supervisor=True)
            shipment.order_id._packing_lock()
            if shipment.packing_sync_state != "review":
                continue
            # Reconciliation only: changing review to submitting can never POST.
            shipment.sudo().write({"packing_sync_state": "submitting", "packing_sync_started_at": False})
        return True


class PackingGroup(models.Model):
    _inherit = "fulfillment.shipment.group"

    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            order_id = vals.get("order_id") or self.env.context.get("default_order_id")
            order = self.env["shopify.order"].browse(order_id).exists() if order_id else self.env["shopify.order"]
            if order and order.completion_policy_version == POLICY:
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete":
                    raise PackingError("completed_order", "Completed orders cannot receive replacement shipment groups.")
                order.sudo().write({"packing_version": order.packing_version + 1})
        return super().create(vals_list)

    def write(self, vals):
        if {"order_id", "shipment_ids"}.intersection(vals):
            orders = self.mapped("order_id")
            if vals.get("order_id"):
                orders |= self.env["shopify.order"].browse(vals["order_id"])
            for order in orders.filtered(lambda record: record.completion_policy_version == POLICY).sorted("id"):
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete":
                    raise PackingError("completed_order", "Completed shipment group relationships must be preserved.")
                order.sudo().write({"packing_version": order.packing_version + 1})
        result = super().write(vals)
        for group in self:
            if any(shipment.order_id != group.order_id for shipment in group.shipment_ids):
                raise PackingError("allocation_mismatch", "Every box in this group must belong to its order.")
        return result

    def unlink(self):
        for group in self:
            if group.order_id.completion_policy_version == POLICY:
                group.order_id._packing_authorize(supervisor=True)
                group.order_id._packing_lock()
                if group.order_id.sale_order_id or group.order_id.packing_operational_state == "complete":
                    raise PackingError("delete_forbidden", "Completed shipment groups must be retained.")
            group.shipment_ids._packing_void("Shipment batch removed")
        return super().unlink()


class PackingOrderLine(models.Model):
    _inherit = "shopify.order.line"

    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            order_id = vals.get("order_id") or self.env.context.get("default_order_id")
            order = self.env["shopify.order"].browse(order_id).exists() if order_id else self.env["shopify.order"]
            if order and order.completion_policy_version == POLICY:
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete":
                    raise PackingError("completed_order", "Completed orders cannot receive additional items.")
                order.sudo().write({"packing_version": order.packing_version + 1})
        return super().create(vals_list)

    def write(self, vals):
        sensitive = {"quantity", "sku", "shopify_line_id", "shopify_variant_id", "requires_shipping", "order_id"}.intersection(vals)
        if sensitive:
            orders = self.mapped("order_id")
            if vals.get("order_id"):
                orders |= self.env["shopify.order"].browse(vals["order_id"])
            for order in orders.filtered(lambda record: record.completion_policy_version == POLICY).sorted("id"):
                if not self.env.su:
                    order._packing_authorize(supervisor=True)
                order._packing_lock()
                if order.packing_operational_state == "complete":
                    raise PackingError("completed_order", "Completed order items require reconciliation instead of editing.")
                order.sudo().write({"packing_version": order.packing_version + 1})
        return super().write(vals)

    def unlink(self):
        for order in self.mapped("order_id").filtered(lambda record: record.completion_policy_version == POLICY):
            if not self.env.su:
                order._packing_authorize(supervisor=True)
            order._packing_lock()
            if order.packing_operational_state == "complete":
                raise PackingError("completed_order", "Completed order items must be retained.")
            order.sudo().write({"packing_version": order.packing_version + 1})
        return super().unlink()
