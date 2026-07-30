from unittest.mock import Mock, patch

from odoo import exceptions
from odoo.tests.common import BaseCase, tagged

from ..services import shopify_api
from ..services.shopify_api import ShopifyAPI


@tagged("post_install", "-at_install")
class TestShopifyInventoryTransfer(BaseCase):
    @staticmethod
    def _response(payload, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    @patch.object(shopify_api.requests, "post")
    @patch.object(shopify_api.requests, "get")
    def test_paired_adjustment_is_guarded_and_idempotent(self, mock_get, mock_post):
        mock_get.side_effect = [
            self._response({"variant": {"inventory_item_id": 999}}),
            self._response({
                "inventory_levels": [
                    {"location_id": 101, "available": 20},
                    {"location_id": 202, "available": 4},
                ],
            }),
        ]
        mock_post.return_value = self._response({
            "data": {
                "inventoryAdjustQuantities": {
                    "userErrors": [],
                    "inventoryAdjustmentGroup": {
                        "referenceDocumentUri": "gid://test/Restock/7",
                    },
                },
            },
        })
        api = ShopifyAPI("example.myshopify.com", "token", "2024-01")

        result = api.transfer_available_inventory(
            variant_id="gid://shopify/ProductVariant/55",
            quantity=3,
            source_location_id="101",
            destination_location_id="gid://shopify/Location/202",
            reference_uri="gid://test/Restock/7",
        )

        self.assertEqual(result["source_after"], 17)
        self.assertEqual(result["destination_after"], 7)
        self.assertIn("/admin/api/2026-01/graphql.json", mock_post.call_args.args[0])
        variables = mock_post.call_args.kwargs["json"]["variables"]
        self.assertEqual(
            variables["input"]["changes"],
            [
                {
                    "delta": -3,
                    "changeFromQuantity": 20,
                    "inventoryItemId": "gid://shopify/InventoryItem/999",
                    "locationId": "gid://shopify/Location/101",
                },
                {
                    "delta": 3,
                    "changeFromQuantity": 4,
                    "inventoryItemId": "gid://shopify/InventoryItem/999",
                    "locationId": "gid://shopify/Location/202",
                },
            ],
        )
        self.assertEqual(36, len(variables["idempotencyKey"]))

    @patch.object(shopify_api.requests, "post")
    @patch.object(shopify_api.requests, "get")
    def test_source_shortage_blocks_adjustment(self, mock_get, mock_post):
        mock_get.side_effect = [
            self._response({"variant": {"inventory_item_id": 999}}),
            self._response({
                "inventory_levels": [
                    {"location_id": 101, "available": 0},
                    {"location_id": 202, "available": 4},
                ],
            }),
        ]
        api = ShopifyAPI("example.myshopify.com", "token", "2026-01")

        with self.assertRaisesRegex(
            exceptions.UserError,
            "Shopify Fulfillment has 0 available, but 3 are required",
        ):
            api.transfer_available_inventory(
                variant_id="55",
                quantity=3,
                source_location_id="101",
                destination_location_id="202",
                reference_uri="gid://test/Restock/7",
            )

        mock_post.assert_not_called()

    @patch.object(shopify_api.requests, "post")
    @patch.object(shopify_api.requests, "get")
    def test_destination_mismatch_blocks_adjustment(self, mock_get, mock_post):
        mock_get.side_effect = [
            self._response({"variant": {"inventory_item_id": 999}}),
            self._response({
                "inventory_levels": [
                    {"location_id": 101, "available": 20},
                    {"location_id": 202, "available": 4},
                ],
            }),
        ]
        api = ShopifyAPI("example.myshopify.com", "token", "2026-01")

        with self.assertRaisesRegex(
            exceptions.UserError,
            "Shopify Retail would become 7, but Odoo Retail currently has 9",
        ):
            api.transfer_available_inventory(
                variant_id="55",
                quantity=3,
                source_location_id="101",
                destination_location_id="202",
                reference_uri="gid://test/Restock/7",
                expected_destination_after=9,
            )

        mock_post.assert_not_called()
