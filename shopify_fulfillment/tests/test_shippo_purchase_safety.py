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
    ("shopify_fulfillment.services.address_review", "address_review.py"),
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

    @patch.object(shippo_service.ShippoService, "_post_rate_request")
    def test_rate_response_preserves_submitted_and_validated_destinations(self, post):
        post.return_value = _Response(200, payload={
            "address_to": {
                "street1": "2 Richard St", "city": "Cove", "state": "TX",
                "zip": "77520-3962", "country": "US",
                "validation_results": {"is_valid": True, "messages": [{"type": "address_correction"}]},
            },
            "rates": [self.rate],
        })
        order = types.SimpleNamespace(
            id=8634, customer_name="H Boyer", shipping_address_line1="2 Richard Pearse Cove",
            shipping_address_line2="", shipping_city="Georgetown", shipping_state="TX",
            shipping_zip="78626", shipping_country="US", shipping_phone="", email="",
        )
        sender = types.SimpleNamespace(
            name="Sender", street="1 Main St", street2="", city="Austin", zip="78701",
            state_id=types.SimpleNamespace(code="TX"),
            country_id=types.SimpleNamespace(code="US"), phone="", email="",
        )
        box = types.SimpleNamespace(name="Box 1", length=10, width=8, height=4)

        rates, meta = self.service.get_rates_for_box(order, box, 100, sender)

        self.assertEqual(rates, [self.rate])
        self.assertEqual(meta["submitted_address"]["street1"], "2 Richard Pearse Cove")
        self.assertEqual(meta["submitted_address"]["zip"], "78626")
        self.assertEqual(meta["validated_address"]["street1"], "2 Richard St")
        self.assertEqual(meta["validated_address"]["zip"], "77520-3962")

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
