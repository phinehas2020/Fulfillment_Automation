"""Disposable Odoo fixtures: no provider purchases, print jobs or HTTP calls."""
import json
import uuid
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from ..services.packing_utils import PACKING_MOBILE_REQUEST, PackingError


@tagged("post_install", "-at_install", "packing")
class TestPackingWorkflow(TransactionCase):
    def setUp(self):
        super().setUp()
        self._packing_build_fixture()

    def _packing_build_fixture(self):
        self.company = self.env.company
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        employee = self.env.ref("shopify_fulfillment.group_packing_employee")
        supervisor = self.env.ref("shopify_fulfillment.group_packing_supervisor")
        self.employee = self.env["res.users"].with_context(no_reset_password=True).create({"name": "Packing Fixture Employee", "login": "packing-employee-%s" % uuid.uuid4(), "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])], "groups_id": [(6, 0, [employee.id])], "packing_warehouse_ids": [(6, 0, [self.warehouse.id])]})
        self.other = self.employee.copy({"name": "Other Packing Fixture Employee", "login": "packing-other-%s" % uuid.uuid4()})
        self.env.user.sudo().write({"groups_id": [(4, supervisor.id)]})
        self.product = self.env["product.product"].create({"name": "Packing Fixture Cornmeal", "default_code": "PACKING-%s" % uuid.uuid4(), "is_storable": True, "barcode": "012345678912"})
        self.order = self.env["shopify.order"].sudo().create({"shopify_id": "packing-%s" % uuid.uuid4(), "order_name": "#PACKING-FIXTURE", "state": "ready_to_ship", "completion_policy_version": "packing_v1", "packing_warehouse_id": self.warehouse.id, "shipping_zip": "76705", "raw_payload": json.dumps({"shipping_address": {"address1": "Fixture address", "zip": "76705"}, "line_items": [{"id": 991, "sku": self.product.default_code, "variant_id": 9001, "quantity": 3, "requires_shipping": True}]}), "line_ids": [(0, 0, {"shopify_line_id": "991", "shopify_variant_id": "9001", "sku": self.product.default_code, "title": self.product.name, "variant_title": "5 lb", "quantity": 3})]})
        self.line = self.order.line_ids
        self.group = self.env["fulfillment.shipment.group"].create({"order_id": self.order.id, "state": "complete"})
        self.shipments = self.env["fulfillment.shipment"].sudo().create([{"order_id": self.order.id, "group_id": self.group.id, "tracking_number": "9400100000000000000001", "carrier": "USPS", "purchase_state": "purchased", "sequence": 1, "line_ids": [(6, 0, self.line.ids)], "line_quantities": json.dumps({str(self.line.id): 1}), "label_zpl": "^BCN,100,N,N,N,D^FD420767050001>89400100000000000000001^FS"}, {"order_id": self.order.id, "group_id": self.group.id, "tracking_number": "1Z0000000000000001", "carrier": "UPS", "purchase_state": "purchased", "sequence": 2, "line_ids": [(6, 0, self.line.ids)], "line_quantities": json.dumps({str(self.line.id): 2}), "label_zpl": "^BCN,100,N,N,N,A^FV1Z0000000000000001^FS"}])
        self.order.sudo().write({"shipment_group_id": self.group.id, "shipment_id": self.shipments[0].id})
        self.adapter = Mock()
        self.adapter.get_orders.return_value = [{"id": self.order.shopify_id, "financial_status": "paid", "shipping_address": {"address1": "Fixture address", "zip": "76705"}, "line_items": [{"id": 991, "sku": self.product.default_code, "variant_id": 9001, "quantity": 3, "requires_shipping": True}]}]
        self.adapter.get_risk_level.return_value = "LOW"
        self.adapter._get_fulfillable_orders.return_value = [{"id": 44, "line_items": [{"id": 55, "line_item_id": 991, "fulfillable_quantity": 3}]}]
        self.adapter.get_order_fulfillments.return_value = []
        self.api_patch = patch.object(type(self.order), "_get_shopify_api", autospec=True, return_value=self.adapter)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def event(self):
        return str(uuid.uuid4())

    def shipment(self, index=0, user=None):
        return self.shipments[index].with_user(user or self.employee).with_company(self.company).with_context(mobile_device_id="fixture-device", _packing_mobile_request=PACKING_MOBILE_REQUEST)

    def quantity(self, index, quantity, event=None):
        shipment = self.shipment(index)
        return shipment.packing_set_quantity(self.line.id, quantity, shipment.order_id.packing_version, event or self.event(), "fixture-device")

    def complete(self, index, event=None):
        shipment = self.shipment(index)
        return shipment.packing_complete(shipment.tracking_number, "code128", shipment.order_id.packing_version, event or self.event(), "fixture-device")

    def test_resolve_exact_carrier_alias_and_no_business_changes(self):
        before = (self.order.packing_version, self.order.sale_order_id.id, self.order.inventory_deducted)
        detail = self.env["fulfillment.shipment"].with_user(self.employee).packing_resolve("420767050001\x1d9400100000000000000001", "code128")
        self.assertEqual(detail["shipment_id"], self.shipments[0].id)
        self.assertEqual(detail["items"][0]["required_quantity"], 1)
        self.assertEqual(before, (self.order.packing_version, self.order.sale_order_id.id, self.order.inventory_deducted))
        self.adapter.create_fulfillment.assert_not_called()

    def test_absolute_progress_receipt_and_payload_binding(self):
        event = self.event()
        shipment = self.shipment()
        version = self.order.packing_version
        first = shipment.packing_set_quantity(self.line.id, 1, version, event, "fixture-device")
        repeated = shipment.packing_set_quantity(self.line.id, 1, version, event, "fixture-device")
        self.assertEqual(first, repeated)
        self.assertEqual(json.loads(self.shipments[0].packing_confirmed_json), {str(self.line.id): 1})
        with self.assertRaisesRegex(PackingError, "different action"):
            shipment.packing_set_quantity(self.line.id, 0, version, event, "fixture-device")
        with self.assertRaisesRegex(PackingError, "authenticated device"):
            shipment.packing_set_quantity(self.line.id, 1, version, event, "other-device")
        with self.assertRaisesRegex(PackingError, "different action"):
            self.shipment(user=self.other).packing_set_quantity(self.line.id, 1, version, event, "fixture-device")
        self.assertEqual(self.env["fulfillment.packing.receipt"].sudo().search_count([("event_id", "=", event)]), 1)

    def test_claim_and_stale_version_conflicts(self):
        initial = self.order.packing_version
        self.quantity(0, 1)
        with self.assertRaisesRegex(PackingError, "Refresh"):
            self.shipment(1).packing_set_quantity(self.line.id, 2, initial, self.event(), "fixture-device")
        with self.assertRaisesRegex(PackingError, "being packed"):
            self.shipment(1, self.other).with_context(mobile_device_id="other-device").packing_set_quantity(self.line.id, 2, self.order.packing_version, self.event(), "other-device")

    def test_all_boxes_gate_one_finalizer_and_no_inline_shopify(self):
        task_type = type(self.env["project.task"])
        def finalize(task):
            task.fulfillment_inventory_deducted = True
            return self.env["sale.order"]
        with patch.object(task_type, "_finalize_sale_delivery_accounting_strict", autospec=True, side_effect=finalize) as finalizer:
            self.quantity(0, 1)
            first = self.complete(0)
            self.assertEqual(first["packing_state"], "packed")
            self.assertEqual(first["operational_state"], "pending")
            finalizer.assert_not_called()
            self.quantity(1, 2)
            event, version = self.event(), self.order.packing_version
            shipment = self.shipment(1)
            result = shipment.packing_complete(shipment.tracking_number, "code128", version, event, "fixture-device")
            repeated = shipment.packing_complete(shipment.tracking_number, "code128", version, event, "fixture-device")
            self.assertEqual(result, repeated)
            self.assertEqual(result["operational_state"], "complete")
            self.assertEqual(finalizer.call_count, 1)
            self.assertEqual(set(self.shipments.mapped("packing_sync_state")), {"pending"})
            self.adapter.create_fulfillment.assert_not_called()

    def test_finalizer_failure_rolls_back_local_effects_preserves_packing(self):
        def fail(task):
            self.env["res.partner"].create({"name": "SHOULD-ROLL-BACK-PACKING-FIXTURE"})
            task.fulfillment_inventory_deducted = True
            raise UserError("Fixture accounting failure")
        self.quantity(0, 1)
        self.complete(0)
        self.quantity(1, 2)
        with patch.object(type(self.env["project.task"]), "_finalize_sale_delivery_accounting_strict", autospec=True, side_effect=fail):
            result = self.complete(1)
        self.assertEqual(result["packing_state"], "packed")
        self.assertEqual(result["operational_state"], "review")
        self.assertFalse(self.order.inventory_deducted)
        self.assertFalse(self.env["res.partner"].search_count([("name", "=", "SHOULD-ROLL-BACK-PACKING-FIXTURE")]))
        self.assertEqual(set(self.shipments.mapped("packing_sync_state")), {"not_ready"})

    def test_wrong_label_and_incomplete_quantity_cannot_complete(self):
        self.quantity(0, 1)
        with self.assertRaisesRegex(PackingError, "same label"):
            self.shipment().packing_complete(self.shipments[1].tracking_number, "code128", self.order.packing_version, self.event(), "fixture-device")
        self.quantity(0, 0)
        with self.assertRaisesRegex(PackingError, "every required item"):
            self.complete(0)
        self.assertNotEqual(self.shipments[0].packing_state, "packed")

    def test_changed_cancelled_or_risky_shopify_order_is_held(self):
        self.quantity(0, 1)
        for mutation in ({"cancelled_at": "2026-10-05"}, {"financial_status": "refunded"}, {"financial_status": "pending"}, {"financial_status": "authorized"}, {"financial_status": "partially_paid"}, {"line_items": [{"id": 991, "quantity": 2, "sku": self.product.default_code, "variant_id": 9001, "requires_shipping": True}]}):
            old = dict(self.adapter.get_orders.return_value[0])
            self.adapter.get_orders.return_value[0].update(mutation)
            with self.assertRaises(PackingError):
                self.complete(0)
            self.adapter.get_orders.return_value = [old]
        self.adapter.get_risk_level.return_value = "HIGH"
        with self.assertRaisesRegex(PackingError, "risk"):
            self.complete(0)
        self.assertNotEqual(self.shipments[0].packing_state, "packed")

    def test_missing_allocations_or_product_mapping_block_actions(self):
        self.shipments[1].sudo().write({"line_quantities": "{}"})
        with self.assertRaises(PackingError):
            self.quantity(0, 1)
        self.shipments[1].sudo().write({"line_quantities": json.dumps({str(self.line.id): 2})})
        self.product.default_code = "DIFFERENT-FIXTURE-SKU"
        with self.assertRaisesRegex(PackingError, "mapping"):
            self.quantity(0, 1)

    def test_direct_progress_and_task_done_are_denied(self):
        with self.assertRaises(AccessError):
            self.shipment().write({"packing_state": "packed"})
        task = self.order.sudo().ensure_fulfillment_task()
        with self.assertRaisesRegex(UserError, "Complete each box"):
            task.with_user(self.employee).write({"state": "1_done"})

    def test_unauthorized_warehouse_and_company_cannot_read_or_write(self):
        outsider = self.employee.copy({"name": "No Packing Warehouse", "login": "packing-outsider-%s" % uuid.uuid4(), "packing_warehouse_ids": [(5, 0, 0)]})
        with self.assertRaisesRegex(PackingError, "outside your packing"):
            self.shipment(user=outsider).packing_detail()
        company = self.env["res.company"].create({"name": "Packing Fixture Other Company"})
        self.order.sudo().write({"company_id": company.id})
        with self.assertRaises(Exception) as denied:
            self.shipment().packing_detail()
        self.assertIsInstance(denied.exception, (PackingError, AccessError))

    def test_void_tombstone_survives_record_removal(self):
        tracking = self.shipments[0].tracking_number
        self.shipments[0].unlink()
        labels = self.env["fulfillment.packing.label"].sudo().search([("barcode", "=", tracking)])
        self.assertEqual(labels.state, "void")
        self.assertFalse(labels.shipment_id)
        with self.assertRaisesRegex(PackingError, "voided"):
            self.env["fulfillment.shipment"].with_user(self.employee).packing_resolve(tracking)

    def test_historical_skip_shopify_requires_exact_external_match(self):
        self.order.sudo().write({"completion_policy_version": "legacy_print", "state": "shipped"})
        with self.assertRaisesRegex(PackingError, "Already-notified"):
            self.order.action_packing_adopt(self.warehouse.id, "Verified fixture physical queue")
        with self.assertRaisesRegex(PackingError, "uniquely matched"):
            self.order.action_packing_adopt(self.warehouse.id, "Verified fixture physical queue", skip_shopify=True)
        self.adapter.get_order_fulfillments.return_value = [{"id": 100 + index, "status": "success", "tracking_number": shipment.tracking_number, "line_items": [{"id": 991, "quantity": index + 1}]} for index, shipment in enumerate(self.shipments)]
        self.order.action_packing_adopt(self.warehouse.id, "Verified exact tracking/items; local delivery still pending", skip_shopify=True)
        self.assertTrue(self.order.packing_skip_shopify)
        self.assertEqual(set(self.shipments.mapped("packing_sync_state")), {"synced"})
        self.assertEqual(set(self.shipments.mapped("packing_state")), {"unstarted"})
        self.assertFalse(self.order.sale_order_id)

    def test_print_policy_cannot_return_to_old_trigger_after_cutover(self):
        self.assertFalse(self.order._packing_print_finalizes())
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_enabled", "False")
        self.assertFalse(self.order._packing_print_finalizes())
        self.order.sudo().write({"completion_policy_version": "legacy_print"})
        self.assertTrue(self.order._packing_print_finalizes())
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_enabled", "True")
        self.assertFalse(self.order._packing_print_finalizes())

    def test_native_disabled_odoo_fallback_keeps_policy(self):
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_native_enabled", "False")
        self.quantity(0, 1)
        self.assertEqual(self.order.completion_policy_version, "packing_v1")
        self.assertFalse(self.order._packing_print_finalizes())

    def test_protected_creation_task_flags_and_sync_dispatch_are_denied(self):
        with self.assertRaises(AccessError):
            self.env["shopify.order"].with_user(self.employee).create({"shopify_id": "forged-%s" % uuid.uuid4(), "packing_operational_state": "complete"})
        with self.assertRaises(AccessError):
            self.env["fulfillment.shipment"].with_user(self.employee).create({"order_id": self.order.id, "packing_state": "packed"})
        task = self.order.sudo().ensure_fulfillment_task()
        with self.assertRaises(AccessError):
            task.with_user(self.employee).write({"fulfillment_inventory_deducted": True})
        with self.assertRaises(AccessError):
            self.env["project.task"].with_user(self.employee).create({"name": "Forged Inventory Flag", "fulfillment_inventory_deducted": True})
        with self.assertRaises(AccessError):
            self.env["project.task"].with_user(self.employee).with_context(default_shopify_order_id=self.order.id, default_is_fulfillment_task=True, default_state="1_done").create({"name": "Forged Context Done Task"})
        with self.assertRaises(AccessError):
            self.env["fulfillment.shipment"].with_user(self.employee).cron_sync_packed_shipments()

    def test_forged_or_stale_inventory_flag_never_proves_business_completion(self):
        task = self.order.sudo().ensure_fulfillment_task()
        task.fulfillment_inventory_deducted = True
        with self.assertRaisesRegex(UserError, "lacks verified"):
            task._finalize_sale_delivery_accounting_strict()
        self.assertFalse(self.order.sale_order_id)

    def test_order_line_edit_invalidates_progress_version_and_blocks_employee(self):
        version = self.order.packing_version
        with self.assertRaises(PackingError):
            self.line.with_user(self.employee).write({"quantity": 2})
        self.line.sudo().write({"quantity": 2})
        self.assertGreater(self.order.packing_version, version)
        with self.assertRaisesRegex(PackingError, "Refresh"):
            self.shipment().packing_set_quantity(self.line.id, 1, version, self.event(), "fixture-device")

    def test_historical_preview_derives_warehouse_without_writes(self):
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_warehouse_id", str(self.warehouse.id))
        self.order.sudo().write({"completion_policy_version": "legacy_print", "packing_warehouse_id": False})
        version = self.order.packing_version
        detail = self.env["fulfillment.shipment"].with_user(self.employee).packing_resolve(self.shipments[0].tracking_number)
        self.assertFalse(any(detail["allowed_actions"].values()))
        self.assertEqual(detail["workflow_version"], "legacy_print")
        self.assertFalse(self.order.packing_warehouse_id)
        self.assertEqual(self.order.packing_version, version)

    def test_packing_task_cannot_be_unlinked_from_canonical_order(self):
        task = self.order.sudo().ensure_fulfillment_task()
        with self.assertRaises(AccessError):
            task.with_user(self.employee).write({"is_fulfillment_task": False, "shopify_order_id": False})

    def test_receipt_retry_reports_current_voided_state_without_replaying_effects(self):
        event = self.event()
        version = self.order.packing_version
        first = self.shipment().packing_set_quantity(self.line.id, 1, version, event, "fixture-device")
        self.shipments[0]._packing_void("Fixture supervisor void")
        repeated = self.shipment().packing_set_quantity(self.line.id, 1, version, event, "fixture-device")
        self.assertEqual(repeated["packing_state"], "void")
        self.assertFalse(any(repeated["allowed_actions"].values()))
        self.assertNotEqual(first["packing_state"], repeated["packing_state"])

    def test_rpc_context_cannot_forge_phone_device_provenance(self):
        shipment = self.shipments[0].with_user(self.employee).with_context(mobile_device_id="fixture-device")
        with self.assertRaisesRegex(PackingError, "authenticated packing API"):
            shipment.packing_set_quantity(self.line.id, 1, self.order.packing_version, self.event(), "fixture-device")

    def test_canonical_task_and_order_retained_instead_of_cascade_deletion(self):
        task = self.order.sudo().ensure_fulfillment_task()
        with self.assertRaisesRegex(UserError, "must be retained"):
            task.sudo().unlink()
        with self.assertRaisesRegex(UserError, "Archive"):
            self.order.sudo().unlink()
        self.assertTrue(task.exists())
        self.assertTrue(self.order.exists())
        self.assertTrue(self.shipments.exists())

    def test_new_box_creation_invalidates_version_and_rejects_completed_parent(self):
        version = self.order.packing_version
        values = {"order_id": self.order.id, "group_id": self.group.id, "tracking_number": "1ZNEWFIXTURE", "carrier": "UPS", "line_ids": [(6, 0, self.line.ids)], "line_quantities": json.dumps({str(self.line.id): 1})}
        self.env["fulfillment.shipment"].sudo().create(values)
        self.assertGreater(self.order.packing_version, version)
        self.order.sudo().write({"packing_operational_state": "complete"})
        with self.assertRaisesRegex(PackingError, "cannot receive additional"):
            self.env["fulfillment.shipment"].sudo().create(dict(values, tracking_number="1ZAFTERDONEFIXTURE"))

    def test_supervised_manual_import_assigns_warehouse_before_creating_lines(self):
        supervisor = self.employee.copy({"name": "Packing Fixture Import Supervisor", "login": "packing-import-supervisor-%s" % uuid.uuid4(), "groups_id": [(4, self.env.ref("shopify_fulfillment.group_packing_supervisor").id)]})
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_enabled", "True")
        self.env["ir.config_parameter"].sudo().set_param("fulfillment.packing_warehouse_id", str(self.warehouse.id))
        order = self.env["shopify.order"].with_user(supervisor).create({"shopify_id": "manual-import-%s" % uuid.uuid4(), "line_ids": [(0, 0, {"shopify_line_id": "991", "sku": self.product.default_code, "title": self.product.name, "quantity": 1})]})
        self.assertEqual(order.completion_policy_version, "packing_v1")
        self.assertEqual(order.packing_warehouse_id, self.warehouse)
        self.assertEqual(len(order.line_ids), 1)
