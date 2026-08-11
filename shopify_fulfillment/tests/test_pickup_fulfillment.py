from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPickupFulfillment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "fulfillment.default_user_id", str(self.env.user.id)
        )
        self.order = self.env["shopify.order"].create({
            "shopify_id": "pickup-test-43001",
            "order_name": "#43001",
            "customer_name": "Pickup Customer",
            "source": "shopify",
            "fulfillment_type": "pickup",
            "pickup_notification_state": "queued",
            "line_ids": [(0, 0, {
                "shopify_line_id": "9001",
                "sku": "1320B",
                "title": "White Cornmeal",
                "variant_title": "10 lb",
                "quantity": 2,
                "requires_shipping": True,
            })],
        })

    def test_pickup_creates_one_assigned_task_and_no_shipping_artifacts(self):
        order_model = type(self.order)
        with patch.object(
            order_model,
            "_confirm_pickup_delivery_method",
            autospec=True,
            return_value=True,
        ), patch.object(
            order_model,
            "_send_pickup_notification",
            autospec=True,
            return_value=True,
        ):
            self.order._process_order_inner()
            self.order._process_order_inner()

        tasks = self.env["project.task"].search([
            ("shopify_order_id", "=", self.order.id),
            ("is_fulfillment_task", "=", True),
        ])
        self.assertEqual(len(tasks), 1)
        self.assertIn(self.env.user, tasks.user_ids)
        self.assertEqual(self.order.state, "pickup_pending")
        self.assertFalse(self.order.shipment_id)
        self.assertFalse(self.order.shipment_group_id)
        self.assertFalse(self.order.print_job_ids)

    def test_classifies_native_pickup_and_ambiguous_physical_order(self):
        pickup_vals = self.env["shopify.order"]._fulfillment_classification_vals({
            "shipping_lines": [{"title": "Pickup in store"}],
            "line_items": [{"requires_shipping": True}],
        })
        self.assertEqual(pickup_vals["fulfillment_type"], "pickup")
        self.assertEqual(pickup_vals["pickup_notification_state"], "queued")

        ambiguous_vals = self.env["shopify.order"]._fulfillment_classification_vals({
            "line_items": [{"requires_shipping": True}],
        })
        self.assertEqual(ambiguous_vals["state"], "manual_required")
