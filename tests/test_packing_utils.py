"""Offline tests using synthetic carrier shapes observed in purchased labels."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
exceptions = types.ModuleType("odoo.exceptions")
exceptions.UserError = type("UserError", (Exception,), {})
spec = importlib.util.spec_from_file_location("packing_utils", ROOT / "shopify_fulfillment/services/packing_utils.py")
utils = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"odoo": types.ModuleType("odoo"), "odoo.exceptions": exceptions}):
    spec.loader.exec_module(utils)


class TestPackingUtils(unittest.TestCase):
    def test_preserves_tracking_zeros(self):
        self.assertEqual(utils.canonical_barcode(" 0012 3456 "), "00123456")

    def test_usps_gs1_exact_saved_field(self):
        tracking = "9400100000000000000001"
        prefix = "420767050001"
        zpl = "^XA^BCN,100,N,N,N,D^FD%s>8%s^FS^XZ" % (prefix, tracking)
        self.assertEqual(utils.saved_label_barcodes(zpl, tracking), sorted([tracking, prefix + tracking]))
        self.assertEqual(utils.canonical_barcode("]C1" + prefix + "\x1d" + tracking), prefix + tracking)

    def test_ups_fv_and_unrelated_route(self):
        tracking = "1Z0000000000000001"
        zpl = "^BCN,100,N,N,N,A^FV%s^FS^BCN,100,N,N,N,A^FV123456789012^FS" % tracking
        self.assertEqual(utils.saved_label_barcodes(zpl, tracking), [tracking])

    def test_fedex_code_c_exact_whole_field(self):
        tracking = "000000000001"
        barcode = "9622000000000000000000" + tracking
        zpl = "^BCN,100,N,N,N,N^FWN^FD>;%s^FS" % barcode
        self.assertEqual(utils.saved_label_barcodes(zpl, tracking), sorted([tracking, barcode]))

    def test_never_guess_unsupported_invocations(self):
        self.assertEqual(utils.saved_label_barcodes("^BCN,100^FD>;999>6000000000001^FS", "000000000001"), ["000000000001"])

    def test_reject_invalid_quantity_map(self):
        for value in ('{}', '{"1":true}', '{"1":1.5}', '{"1":"2"}', '{"01":2}', '{"1":0}', '[1]'):
            with self.subTest(value=value), self.assertRaises(utils.PackingError):
                utils.quantity_map(value)

    def test_split_sku_allocations_sum_exactly(self):
        self.assertTrue(utils.validate_allocations({1: 3, 2: 1}, [{1: 1}, {1: 2, 2: 1}]))
        with self.assertRaises(utils.PackingError):
            utils.validate_allocations({1: 3}, [{1: 1}, {1: 1}])
        with self.assertRaises(utils.PackingError):
            utils.validate_allocations({1: 3}, [{1: 3, 2: 1}])

    def test_external_reconciliation_requires_exact_tracking_and_items(self):
        fulfillment = {"id": 12, "status": "success", "tracking_number": "0012", "line_items": [{"id": 91, "quantity": 1}]}
        self.assertTrue(utils.fulfillment_matches(fulfillment, "00 12", [{"shopify_line_id": "91", "quantity": 1}]))
        self.assertFalse(utils.fulfillment_matches(fulfillment, "0012", [{"shopify_line_id": "91", "quantity": 2}]))
        self.assertFalse(utils.fulfillment_matches(fulfillment, "12", [{"shopify_line_id": "91", "quantity": 1}]))
        self.assertFalse(utils.fulfillment_matches(dict(fulfillment, status="cancelled"), "0012", [{"shopify_line_id": "91", "quantity": 1}]))

    def test_event_binding_includes_payload(self):
        self.assertNotEqual(utils.payload_digest({"quantity": 1}), utils.payload_digest({"quantity": 2}))
        self.assertEqual(utils.event_identity("00000000-0000-0000-0000-000000000001", "test-device"), ("00000000-0000-0000-0000-000000000001", "test-device"))
        with self.assertRaises(utils.PackingError):
            utils.event_identity("not-uuid", "test-device")


if __name__ == "__main__":
    unittest.main()
