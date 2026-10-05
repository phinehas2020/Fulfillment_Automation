"""Real sale, stock, invoice and payment operations in disposable fixtures."""
import json
from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .test_packing_workflow import TestPackingWorkflow


@tagged("post_install", "-at_install", "packing")
class TestPackingBusinessEffects(AccountTestInvoicingCommon):
    # Reuse fixture construction and API mocks, but run actual business methods.
    _packing_build_fixture = TestPackingWorkflow._packing_build_fixture
    event = TestPackingWorkflow.event
    shipment = TestPackingWorkflow.shipment
    quantity = TestPackingWorkflow.quantity
    complete = TestPackingWorkflow.complete

    def setUp(self):
        super().setUp()
        self._packing_build_fixture()

    def _prepare_accounting(self):
        self.product.property_account_income_id = self.company_data["default_account_revenue"]
        self.product.property_account_expense_id = self.company_data["default_account_expense"]
        self.product.taxes_id = False
        payload = self.order._payload_dict()
        payload["line_items"][0]["price"] = "10.00"
        self.order.sudo().write({"raw_payload": json.dumps(payload)})
        self.adapter.get_orders.return_value[0]["line_items"][0]["price"] = "10.00"
        self.env["stock.quant"]._update_available_quantity(self.product, self.warehouse.lot_stock_id, 10)

    def test_real_delivery_invoice_payment_and_receipt_retry_once(self):
        self._prepare_accounting()
        self.quantity(0, 1)
        self.complete(0)
        self.assertFalse(self.order.sale_order_id)
        self.quantity(1, 2)
        event, version = self.event(), self.order.packing_version
        shipment = self.shipment(1)
        result = shipment.packing_complete(shipment.tracking_number, "code128", version, event, "fixture-device")
        self.assertEqual(result["operational_state"], "complete", result.get("review_reason"))
        sale = self.order.sale_order_id
        self.assertTrue(sale)
        self.assertEqual(sale.warehouse_id, self.warehouse)
        deliveries = sale.picking_ids.filtered(lambda picking: picking.picking_type_code == "outgoing")
        self.assertTrue(deliveries)
        self.assertEqual(set(deliveries.mapped("state")), {"done"})
        self.assertEqual(sum(deliveries.move_ids.filtered(lambda move: move.product_id == self.product).mapped("quantity")), 3)
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id), 7)
        invoices = sale.invoice_ids.filtered(lambda invoice: invoice.move_type == "out_invoice")
        self.assertTrue(invoices)
        self.assertEqual(set(invoices.mapped("state")), {"posted"})
        self.assertTrue(all(invoice.payment_state in ("paid", "in_payment") and invoice.amount_residual == 0 for invoice in invoices))
        before = (sale.id, deliveries.ids, invoices.ids, self.env["account.payment"].search_count([]), self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id))
        shipment.packing_complete(shipment.tracking_number, "code128", version, event, "fixture-device")
        after = (self.order.sale_order_id.id, sale.picking_ids.ids, sale.invoice_ids.ids, self.env["account.payment"].search_count([]), self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id))
        self.assertEqual(before, after)
        self.adapter.create_fulfillment.assert_not_called()

    def test_real_delivery_is_rolled_back_when_accounting_fails(self):
        self._prepare_accounting()
        self.quantity(0, 1)
        self.complete(0)
        self.quantity(1, 2)
        before = (self.env["sale.order"].search_count([]), self.env["stock.picking"].search_count([]), self.env["account.move"].search_count([]), self.env["account.payment"].search_count([]))
        with patch.object(type(self.env["project.task"]), "_mark_sale_order_paid", autospec=True, side_effect=UserError("Fixture accounting rejected after real delivery")):
            result = self.complete(1)
        self.assertEqual(result["packing_state"], "packed")
        self.assertEqual(result["operational_state"], "review")
        self.assertFalse(self.order.sale_order_id)
        self.assertFalse(self.order.inventory_deducted)
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id), 10)
        after = (self.env["sale.order"].search_count([]), self.env["stock.picking"].search_count([]), self.env["account.move"].search_count([]), self.env["account.payment"].search_count([]))
        self.assertEqual(before, after)
        self.adapter.create_fulfillment.assert_not_called()

    def test_existing_extra_delivery_blocks_duplicate_stock_deduction(self):
        self._prepare_accounting()
        trusted = self.order.sudo().with_context(packing_internal_finalizer=True, packing_warehouse_id=self.warehouse.id)
        sale = trusted._create_sale_order()
        extra = sale.picking_ids.filtered(lambda picking: picking.picking_type_code == "outgoing")[0].copy()
        self.assertTrue(extra)
        task = trusted.ensure_fulfillment_task()
        before = self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id)
        with self.assertRaisesRegex(UserError, "exactly cover"):
            task._finalize_sale_delivery_accounting_strict()
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(self.product, self.warehouse.lot_stock_id), before)
        self.assertFalse(task.fulfillment_inventory_deducted)
