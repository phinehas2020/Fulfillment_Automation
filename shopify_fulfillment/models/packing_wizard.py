"""Odoo fallback using exactly the phone's authenticated packing operations."""
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError


class PackingWizard(models.TransientModel):
    _name = "fulfillment.packing.wizard"
    _description = "Pick and Confirm Shipping Box"

    shipment_id = fields.Many2one("fulfillment.shipment", required=True, readonly=True)
    order_name = fields.Char(readonly=True)
    box_number = fields.Integer(readonly=True)
    expected_version = fields.Integer(required=True, readonly=True)
    finish_barcode = fields.Char(string="Scan the box label to finish")
    line_ids = fields.One2many("fulfillment.packing.wizard.line", "wizard_id", string="Items to Pick")
    review_reason = fields.Text(readonly=True)

    def _apply_quantities(self):
        self.ensure_one()
        shipment = self.shipment_id
        current = shipment.packing_detail()
        if current["version"] != self.expected_version:
            raise UserError("This order changed. Close and reopen Packing before continuing.")
        version = self.expected_version
        for line in self.line_ids:
            if line.confirmed_quantity != line.previous_quantity:
                detail = shipment.packing_set_quantity(line.order_line_id.id, line.confirmed_quantity, version, str(uuid.uuid4()), "odoo:%s" % self.env.user.id)
                version = detail["version"]
                line.previous_quantity = line.confirmed_quantity
        self.expected_version = version
        return version

    def action_save(self):
        self._apply_quantities()
        return {"type": "ir.actions.act_window_close"}

    def action_finish(self):
        self.ensure_one()
        if not self.finish_barcode:
            raise UserError("Scan or enter the same shipping label attached to this box.")
        version = self._apply_quantities()
        result = self.shipment_id.packing_complete(self.finish_barcode, "manual", version, str(uuid.uuid4()), "odoo:%s" % self.env.user.id)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {"title": "Box Packed" if result["operational_state"] != "review" else "Packed — Review Needed", "message": result.get("review_reason") or "Confirmed packing saved. Shipping synchronization follows successful delivery and accounting.", "type": "warning" if result["operational_state"] == "review" else "success", "sticky": result["operational_state"] == "review", "next": {"type": "ir.actions.act_window_close"}}}


class PackingWizardLine(models.TransientModel):
    _name = "fulfillment.packing.wizard.line"
    _description = "Picking Checklist Item"

    wizard_id = fields.Many2one("fulfillment.packing.wizard", required=True, ondelete="cascade")
    order_line_id = fields.Many2one("shopify.order.line", required=True, readonly=True)
    title = fields.Char(readonly=True)
    sku = fields.Char(readonly=True)
    required_quantity = fields.Integer(readonly=True)
    confirmed_quantity = fields.Integer()
    previous_quantity = fields.Integer(readonly=True)


class PackingShipmentFallback(models.Model):
    _inherit = "fulfillment.shipment"

    def action_packing_open(self):
        self.ensure_one()
        detail = self.packing_detail()
        if not detail["allowed_actions"]["edit"]:
            raise UserError(detail.get("review_reason") or "This box is completed, held or owned by another employee.")
        wizard = self.env["fulfillment.packing.wizard"].create({"shipment_id": self.id, "order_name": detail["order_name"], "box_number": detail["box_number"], "expected_version": detail["version"], "review_reason": detail["review_reason"], "line_ids": [(0, 0, {"order_line_id": item["line_id"], "title": "%s %s" % (item["title"], item["variant_title"]), "sku": item["sku"], "required_quantity": item["required_quantity"], "confirmed_quantity": item["confirmed_quantity"], "previous_quantity": item["confirmed_quantity"]}) for item in detail["items"]]})
        return {"type": "ir.actions.act_window", "name": "Pick This Box", "res_model": "fulfillment.packing.wizard", "res_id": wizard.id, "view_mode": "form", "target": "new"}
