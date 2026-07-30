from unittest.mock import Mock, patch

from odoo.tests.common import TransactionCase, tagged

from ..services.shopify_api import ShopifyAPI


@tagged("post_install", "-at_install")
class TestRestockShopifyReconciliation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.source = self.env["stock.location"].create({
            "name": "TEST Restock Reconciliation Source",
            "usage": "internal",
        })
        self.destination = self.env["stock.location"].create({
            "name": "TEST Restock Reconciliation Destination",
            "usage": "internal",
        })
        self.product = self.env["product.product"].create({
            "name": "TEST Restock Reconciliation Product",
            "default_code": "TEST-RESTOCK-RECONCILIATION",
        })
        self.move = self.env["stock.move"].create({
            "name": "TEST completed restock move",
            "product_id": self.product.id,
            "product_uom_qty": 3,
            "product_uom": self.product.uom_id.id,
            "location_id": self.source.id,
            "location_dest_id": self.destination.id,
            "state": "done",
        })
        self.task = self.env["project.task"].create({
            "name": "TEST restock reconciliation task",
            "state": "01_in_progress",
        })
        self.item = self.env["fulfillment.restock.item"].create({
            "product_title": "TEST Restock Reconciliation Product",
            "variant_title": "Default Title",
            "sku": "TEST-RESTOCK-RECONCILIATION",
            "restock_amount": 3,
            "variant_id_global": "55",
            "shopify_location_id": "202",
            "todo_task_id": self.task.id,
            "inventory_move_id": self.move.id,
            "inventory_transferred": True,
            "is_active_snapshot": False,
            "inventory_transfer_error":
                "Historical Shopify update missing: test fixture.",
        })
        self.task.fulfillment_restock_item_id = self.item

        params = self.env["ir.config_parameter"].sudo()
        params.set_param("fulfillment.restock_source_location_id", self.source.id)
        params.set_param("fulfillment.pos_stock_location_id", self.destination.id)
        params.set_param(
            "fulfillment.restock_shopify_source_location_id", "101"
        )

    @staticmethod
    def _shopify_result():
        return {
            "source_before": 8,
            "source_after": 5,
            "destination_before": 2,
            "destination_after": 5,
        }

    def test_done_retry_reconciles_shopify_without_second_odoo_move(self):
        api = Mock()
        api.transfer_available_inventory.return_value = self._shopify_result()
        move_count = self.env["stock.move"].search_count([])

        with patch.object(ShopifyAPI, "from_env", return_value=api):
            self.task.write({"state": "1_done"})

        self.assertEqual(self.env["stock.move"].search_count([]), move_count)
        self.assertEqual(self.item.inventory_move_id, self.move)
        self.assertEqual(self.move.state, "done")
        self.assertFalse(self.item.inventory_transfer_error)
        api.transfer_available_inventory.assert_called_once_with(
            variant_id="55",
            quantity=3,
            source_location_id="101",
            destination_location_id="202",
            reference_uri=(
                "gid://homestead-gristmill/"
                f"FulfillmentRestockReconciliation/{self.item.id}"
            ),
        )

    def test_mismatched_existing_move_blocks_shopify(self):
        self.item.restock_amount = 4
        api = Mock()

        with patch.object(ShopifyAPI, "from_env", return_value=api):
            self.task.write({"state": "1_done"})

        api.transfer_available_inventory.assert_not_called()
        self.assertIn("moved 3, not 4", self.item.inventory_transfer_error)
        self.assertEqual(self.item.inventory_move_id, self.move)
