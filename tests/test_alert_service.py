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


if __name__ == "__main__":
    unittest.main()
