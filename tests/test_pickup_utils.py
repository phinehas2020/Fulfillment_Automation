import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UTILS_PATH = ROOT / "shopify_fulfillment" / "services" / "pickup_utils.py"

spec = importlib.util.spec_from_file_location("pickup_utils", UTILS_PATH)
pickup_utils = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pickup_utils
spec.loader.exec_module(pickup_utils)


class PickupClassificationTest(unittest.TestCase):
    def test_native_pickup_in_store_is_pickup(self):
        payload = {
            "shipping_lines": [{"title": "Pickup in store", "code": "Pickup"}],
            "line_items": [{"requires_shipping": True}],
        }
        self.assertTrue(pickup_utils.payload_is_pickup(payload))
        self.assertFalse(
            pickup_utils.payload_has_ambiguous_physical_fulfillment(payload)
        )

    def test_shipping_order_is_not_pickup(self):
        payload = {
            "shipping_lines": [{"title": "UPS Ground"}],
            "shipping_address": {"address1": "608 Dry Creek Road"},
            "line_items": [{"requires_shipping": True}],
        }
        self.assertFalse(pickup_utils.payload_is_pickup(payload))
        self.assertFalse(
            pickup_utils.payload_has_ambiguous_physical_fulfillment(payload)
        )

    def test_addressless_physical_order_requires_manual_review(self):
        payload = {
            "shipping_lines": [],
            "line_items": [{"requires_shipping": True}],
        }
        self.assertFalse(pickup_utils.payload_is_pickup(payload))
        self.assertTrue(
            pickup_utils.payload_has_ambiguous_physical_fulfillment(payload)
        )

    def test_digital_only_order_is_not_ambiguous(self):
        payload = {"line_items": [{"requires_shipping": False}]}
        self.assertFalse(
            pickup_utils.payload_has_ambiguous_physical_fulfillment(payload)
        )

    def test_fulfillment_order_retail_confirms_pickup(self):
        self.assertTrue(
            pickup_utils.fulfillment_orders_confirm_pickup(["RETAIL"])
        )
        self.assertFalse(
            pickup_utils.fulfillment_orders_confirm_pickup(["SHIPPING"])
        )
        self.assertIsNone(pickup_utils.fulfillment_orders_confirm_pickup([]))


if __name__ == "__main__":
    unittest.main()
