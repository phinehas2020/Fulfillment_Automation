"""Standalone destination review policy regression tests."""

import importlib.util
import pathlib
import unittest


MODULE = pathlib.Path(__file__).resolve().parents[1] / "services" / "address_review.py"
spec = importlib.util.spec_from_file_location("address_review", MODULE)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class AddressReviewTest(unittest.TestCase):
    def test_order_44871_georgetown_to_cove_requires_review(self):
        submitted = {
            "street1": "2 Richard Pearse Cove", "city": "Georgetown",
            "state": "TX", "zip": "78626", "country": "US",
        }
        validated = {
            "street1": "2 Richard St", "city": "Cove",
            "state": "TX", "zip": "77520-3962", "country": "US",
        }
        self.assertEqual(
            review.material_address_changes(submitted, validated),
            ["street", "city", "ZIP"],
        )
        self.assertNotEqual(
            review.address_review_fingerprint(submitted, validated),
            review.address_review_fingerprint(submitted, submitted),
        )

    def test_formatting_and_zip_plus_four_do_not_require_review(self):
        submitted = {
            "street1": "  2  Richard St. ", "city": "GEORGETOWN",
            "state": "tx", "zip": "78626", "country": "US",
        }
        validated = {
            "street1": "2 Richard St", "city": "Georgetown",
            "state": "TX", "zip": "78626-1234", "country": "us",
        }
        self.assertEqual(review.material_address_changes(submitted, validated), [])

    def test_missing_or_invalid_validation_holds_purchase(self):
        submitted = {"street1": "2 Richard Pearse Cove", "zip": "78626", "country": "US"}
        self.assertEqual(
            review.material_address_changes(submitted, None),
            ["validated destination unavailable"],
        )
        self.assertEqual(
            review.material_address_changes(submitted, submitted, {"is_valid": False}),
            ["destination failed validation"],
        )


if __name__ == "__main__":
    unittest.main()
