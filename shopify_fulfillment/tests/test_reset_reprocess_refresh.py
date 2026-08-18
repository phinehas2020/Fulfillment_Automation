import json
from unittest.mock import Mock, patch

from odoo import exceptions
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestResetReprocessRefresh(TransactionCase):
    def setUp(self):
        super().setUp()
        self.order = self.env["shopify.order"].create(
            {
                "shopify_id": "9999999999999001",
                "order_number": "TEST-REFRESH",
                "order_name": "#TEST-REFRESH",
                "customer_name": "Old Customer",
                "shipping_address_line1": "Old Street",
                "shipping_city": "Old City",
                "shipping_state": "TX",
                "shipping_zip": "00000",
                "shipping_country": "US",
                "requested_shipping_method": "Old Shipping",
                "state": "error",
            }
        )

    def _shopify_order(self, shipping_address=None):
        return {
            "id": int(self.order.shopify_id),
            "order_number": "TEST-REFRESH",
            "name": "#TEST-REFRESH",
            "email": "updated@example.com",
            "customer": {"first_name": "Updated", "last_name": "Customer"},
            "shipping_address": shipping_address,
            "shipping_lines": [{"title": "Amazon Standard", "price": "44.80"}],
            "line_items": [],
            "source_name": "amazon-us",
            "tags": "",
            "currency": "USD",
            "note_attributes": [
                {"name": "Amazon Order Id", "value": "112-0000000-0000000"},
                {
                    "name": "Amazon Earliest Ship Date",
                    "value": "2026-08-17T07:00:00.000Z",
                },
                {
                    "name": "Amazon Latest Ship Date",
                    "value": "2026-08-18T06:59:59.000Z",
                },
                {
                    "name": "Amazon Earliest Delivery Date",
                    "value": "2026-08-24T07:00:00.000Z",
                },
                {
                    "name": "Amazon Latest Delivery Date",
                    "value": "2026-08-25T06:59:59.000Z",
                },
            ],
        }

    def test_reset_refreshes_shopify_address_before_reprocessing(self):
        api = Mock()
        api.get_orders.return_value = [
            self._shopify_order(
                {
                    "first_name": "Updated",
                    "last_name": "Customer",
                    "address1": "200 Updated Ave",
                    "address2": "Suite 2",
                    "city": "Montgomery",
                    "province_code": "OH",
                    "zip": "45242",
                    "country_code": "US",
                    "phone": "+1 555 0100",
                }
            )
        ]

        with (
            patch.object(
                type(self.order),
                "_get_shopify_api",
                autospec=True,
                return_value=api,
            ),
            patch.object(
                type(self.order), "_reset_fulfillment_state", autospec=True
            ) as reset,
            patch.object(type(self.order), "process_order", autospec=True) as process,
        ):
            self.order.action_reset_and_reprocess()

        api.get_orders.assert_called_once_with([self.order.shopify_id])
        self.assertEqual(self.order.customer_name, "Updated Customer")
        self.assertEqual(self.order.shipping_address_line1, "200 Updated Ave")
        self.assertEqual(self.order.shipping_address_line2, "Suite 2")
        self.assertEqual(self.order.shipping_city, "Montgomery")
        self.assertEqual(self.order.shipping_state, "OH")
        self.assertEqual(self.order.shipping_zip, "45242")
        self.assertEqual(self.order.shipping_country, "US")
        self.assertEqual(self.order.requested_shipping_method, "Amazon Standard")
        self.assertEqual(self.order.source, "amazon")
        self.assertEqual(self.order.amazon_order_id, "112-0000000-0000000")
        self.assertEqual(self.order.shipping_amount_paid, 44.80)
        self.assertEqual(self.order.order_currency, "USD")
        self.assertEqual(
            str(self.order.amazon_latest_delivery_at), "2026-08-25 06:59:59"
        )
        self.assertEqual(
            json.loads(self.order.raw_payload)["id"], int(self.order.shopify_id)
        )
        reset.assert_called_once()
        process.assert_called_once()

    def test_missing_shopify_address_stops_before_reset(self):
        api = Mock()
        api.get_orders.return_value = [self._shopify_order(shipping_address=None)]

        with (
            patch.object(
                type(self.order),
                "_get_shopify_api",
                autospec=True,
                return_value=api,
            ),
            patch.object(
                type(self.order), "_reset_fulfillment_state", autospec=True
            ) as reset,
            patch.object(type(self.order), "process_order", autospec=True) as process,
        ):
            with self.assertRaisesRegex(
                exceptions.UserError,
                "still has no usable shipping address",
            ):
                self.order.action_reset_and_reprocess()

        self.assertEqual(self.order.shipping_address_line1, "Old Street")
        self.assertEqual(self.order.shipping_zip, "00000")
        reset.assert_not_called()
        process.assert_not_called()

    def test_missing_shopify_order_stops_before_reset(self):
        api = Mock()
        api.get_orders.return_value = []

        with (
            patch.object(
                type(self.order),
                "_get_shopify_api",
                autospec=True,
                return_value=api,
            ),
            patch.object(
                type(self.order), "_reset_fulfillment_state", autospec=True
            ) as reset,
            patch.object(type(self.order), "process_order", autospec=True) as process,
        ):
            with self.assertRaisesRegex(
                exceptions.UserError,
                "Shopify did not return order",
            ):
                self.order.action_reset_and_reprocess()

        reset.assert_not_called()
        process.assert_not_called()
