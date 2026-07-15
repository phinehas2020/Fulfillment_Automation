import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "shopify_fulfillment" / "services" / "shippo_service.py"
MODULE_NAME = "shopify_fulfillment.services.shippo_service"

# Load the service without requiring a complete Odoo runtime.
odoo = types.ModuleType("odoo")
odoo.exceptions = types.SimpleNamespace(UserError=RuntimeError)
sys.modules.setdefault("odoo", odoo)

package = sys.modules.setdefault(
    "shopify_fulfillment", types.ModuleType("shopify_fulfillment")
)
package.__path__ = [str(ROOT / "shopify_fulfillment")]
services_package = sys.modules.setdefault(
    "shopify_fulfillment.services", types.ModuleType("shopify_fulfillment.services")
)
services_package.__path__ = [str(ROOT / "shopify_fulfillment" / "services")]

address_utils = types.ModuleType("shopify_fulfillment.services.address_utils")
address_utils.normalize_address_lines = lambda line1, line2: (line1, line2)
sys.modules.setdefault("shopify_fulfillment.services.address_utils", address_utils)

spec = importlib.util.spec_from_file_location(MODULE_NAME, SERVICE_PATH)
shippo_service = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = shippo_service
spec.loader.exec_module(shippo_service)


class SanitizePhoneTest(unittest.TestCase):
    def test_removes_shopify_extension(self):
        self.assertEqual(
            shippo_service.sanitize_phone("+1 415-419-8616 ext. 27208"),
            "+1 415-419-8616",
        )

    def test_removes_common_extension_punctuation(self):
        examples = (
            "+1 415-419-8616 ext: 27208",
            "+1 415-419-8616 ext. #27208",
            "+1 415-419-8616 extension 27208",
            "+1 415-419-8616 x27208",
        )
        for phone in examples:
            with self.subTest(phone=phone):
                self.assertEqual(
                    shippo_service.sanitize_phone(phone),
                    "+1 415-419-8616",
                )


class ShippoRateRetryTest(unittest.TestCase):
    def test_retries_transient_503_then_returns_success(self):
        responses = [
            Mock(status_code=503, text=""),
            Mock(status_code=503, text=""),
            Mock(status_code=200, text='{"rates": []}'),
        ]
        service = shippo_service.ShippoService("test-key")

        with patch.object(shippo_service.requests, "post", side_effect=responses) as post:
            with patch.object(shippo_service.time, "sleep") as sleep:
                response = service._post_rate_request("https://example.test", {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_does_not_retry_validation_error(self):
        service = shippo_service.ShippoService("test-key")
        response_400 = Mock(status_code=400, text="invalid phone")

        with patch.object(
            shippo_service.requests, "post", return_value=response_400
        ) as post:
            with patch.object(shippo_service.time, "sleep") as sleep:
                response = service._post_rate_request("https://example.test", {})

        self.assertIs(response, response_400)
        post.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
