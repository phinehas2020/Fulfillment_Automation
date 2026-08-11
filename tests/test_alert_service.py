import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "shopify_fulfillment" / "services" / "alert_service.py"

spec = importlib.util.spec_from_file_location("alert_service", SERVICE_PATH)
alert_service = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = alert_service
spec.loader.exec_module(alert_service)


class FakeConfigParameters:
    def __init__(self, values):
        self.values = values

    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return self.values.get(key, default)


class FakeEnv:
    def __init__(self, values):
        self.config = FakeConfigParameters(values)

    def __getitem__(self, model_name):
        if model_name != "ir.config_parameter":
            raise KeyError(model_name)
        return self.config


class FakePartner:
    email = "restocker@example.com"


class FakeUser:
    partner_id = FakePartner()
    email = "restocker@example.com"
    display_name = "Restock Employee"


class FakeUsers:
    def __init__(self, users):
        self.users = users

    def __bool__(self):
        return bool(self.users)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return FakeUsers(self.users[item])
        return self.users[item]

    def __getattr__(self, name):
        if not self.users:
            raise AttributeError(name)
        return getattr(self.users[0], name)


class FakeLine:
    requires_shipping = True
    title = "White Cornmeal"
    sku = "1320B"
    variant_title = "10 lb"
    quantity = 2


class FakeOrder:
    shopify_id = "123456"
    order_name = "#43001"
    order_number = "43001"
    customer_name = "Pickup Customer"
    line_ids = [FakeLine()]


class FakeTask:
    id = 42
    user_ids = FakeUsers([FakeUser()])


class TeamsAlertTest(unittest.TestCase):
    def test_sends_message_card_payload_for_teams_workflows(self):
        env = FakeEnv(
            {
                "fulfillment.error_alert_teams_webhook_url":
                    "https://example.test/teams-workflow"
            }
        )
        service = alert_service.AlertService(env)
        response = Mock(status_code=202, text="")

        with patch.object(
            alert_service.requests,
            "post",
            return_value=response,
        ) as post:
            result = service._send_teams(
                subject="[Fulfillment Error] Shippo Failure",
                body_text="Order: #41521\nMessage: No rates",
            )

        self.assertTrue(result)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["@type"], "MessageCard")
        self.assertEqual(payload["@context"], "http://schema.org/extensions")
        self.assertEqual(payload["summary"], "[Fulfillment Error] Shippo Failure")
        self.assertEqual(payload["title"], "[Fulfillment Error] Shippo Failure")
        self.assertEqual(payload["themeColor"], "D13438")
        self.assertEqual(payload["text"], "Order: #41521\nMessage: No rates")

    def test_sends_pickup_workflow_payload_to_assignee_email(self):
        env = FakeEnv(
            {
                "fulfillment.pickup_teams_workflow_url": "https://example.test/pickup",
                "shopify.shop_domain": "homestead-gristmill.myshopify.com",
                "web.base.url": "https://internal.example.com",
            }
        )
        service = alert_service.AlertService(env)
        response = Mock(status_code=202, text="")

        with patch.object(alert_service.requests, "post", return_value=response) as post:
            success, error = service.notify_pickup_assignee(
                order=FakeOrder(), task=FakeTask()
            )

        self.assertTrue(success)
        self.assertEqual(error, "")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["event_type"], "shopify_pickup_order")
        self.assertEqual(payload["recipient_email"], "restocker@example.com")
        self.assertEqual(payload["order_reference"], "#43001")
        self.assertEqual(payload["odoo_task_id"], 42)
        self.assertEqual(payload["items"], ["White Cornmeal - 10 lb x2"])

    def test_pickup_notification_requires_assignee(self):
        env = FakeEnv(
            {"fulfillment.pickup_teams_workflow_url": "https://example.test/pickup"}
        )
        task = FakeTask()
        task.user_ids = FakeUsers([])
        success, error = alert_service.AlertService(env).notify_pickup_assignee(
            order=FakeOrder(), task=task
        )
        self.assertFalse(success)
        self.assertIn("no assigned employee", error)


if __name__ == "__main__":
    unittest.main()
