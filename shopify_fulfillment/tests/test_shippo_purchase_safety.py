"""Standalone safety tests for Shippo label purchase mutations."""

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SERVICES_ROOT = ROOT / "shopify_fulfillment" / "services"

odoo = types.ModuleType("odoo")
odoo.exceptions = types.SimpleNamespace(UserError=RuntimeError)
sys.modules.setdefault("odoo", odoo)
package = sys.modules.setdefault(
    "shopify_fulfillment", types.ModuleType("shopify_fulfillment")
)
package.__path__ = [str(ROOT / "shopify_fulfillment")]
services = sys.modules.setdefault(
    "shopify_fulfillment.services", types.ModuleType("shopify_fulfillment.services")
)
services.__path__ = [str(SERVICES_ROOT)]

for module_name, filename in (
    ("shopify_fulfillment.services.address_utils", "address_utils.py"),
    ("shopify_fulfillment.services.shippo_service", "shippo_service.py"),
):
    spec = importlib.util.spec_from_file_location(module_name, SERVICES_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

shippo_service = sys.modules["shopify_fulfillment.services.shippo_service"]
ShippoService = shippo_service.ShippoService
logging.getLogger(shippo_service.__name__).disabled = True


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class ShippoPurchaseSafetyTest(unittest.TestCase):
    def setUp(self):
        self.service = ShippoService("shippo-test-token")
        self.rate = {
            "object_id": "rate-1",
            "provider": "FedEx",
            "servicelevel": {"name": "Home Delivery", "token": "fedex_ground"},
            "amount": "48.06",
            "currency": "USD",
            "_purchase_reference": "#43359-box-1-intent-99",
        }

    @patch.object(shippo_service.requests, "post")
    def test_timeout_returns_uncertain_and_does_not_retry(self, post):
        post.side_effect = shippo_service.requests.Timeout("timed out")

        result = self.service.purchase_label(self.rate)

        self.assertTrue(result["purchase_uncertain"])
        self.assertEqual(post.call_count, 1)

    @patch.object(shippo_service.requests, "post")
    def test_transient_mutation_response_is_uncertain(self, post):
        post.return_value = _Response(503, text="temporarily unavailable")

        result = self.service.purchase_label(self.rate)

        self.assertTrue(result["purchase_uncertain"])
        self.assertEqual(post.call_count, 1)

    @patch.object(shippo_service.requests, "post")
    def test_queued_or_malformed_success_response_is_uncertain(self, post):
        post.return_value = _Response(
            200,
            payload={"status": "QUEUED", "object_id": "transaction-queued"},
        )
        queued = self.service.purchase_label(self.rate)
        self.assertTrue(queued["purchase_uncertain"])
        self.assertEqual(queued["shippo_transaction_id"], "transaction-queued")

        post.return_value = _Response(200, payload=["unexpected"])
        malformed = self.service.purchase_label(self.rate)
        self.assertTrue(malformed["purchase_uncertain"])

    @patch.object(shippo_service.requests, "post")
    def test_purchase_reference_is_sent_as_metadata(self, post):
        post.return_value = _Response(
            200,
            payload={
                "status": "SUCCESS",
                "object_id": "transaction-1",
                "tracking_number": "tracking-1",
                "tracking_url_provider": "https://example.test/tracking-1",
                "label_url": "https://example.test/label.zpl",
            },
        )

        with patch.object(self.service, "_download_url", return_value="^XA^XZ"):
            result = self.service.purchase_label(self.rate)

        self.assertEqual(result["shippo_transaction_id"], "transaction-1")
        self.assertEqual(
            post.call_args.kwargs["json"]["metadata"],
            "#43359-box-1-intent-99",
        )


if __name__ == "__main__":
    unittest.main()
