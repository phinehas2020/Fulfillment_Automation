import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ADDON_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ADDON_ROOT / "services" / "rate_policy.py"

spec = importlib.util.spec_from_file_location("rate_policy", POLICY_PATH)
rate_policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rate_policy
spec.loader.exec_module(rate_policy)


class RateNormalizationTest(unittest.TestCase):
    def test_shippo_uses_structured_token_and_safe_numeric_fields(self):
        normalized = rate_policy.normalize_shippo_rate({
            "object_id": "shippo-rate-1",
            "carrier_account": "fedex-account-1",
            "provider": "A display name that policy must ignore",
            "servicelevel": {
                "token": "fedex_ground",
                "name": "A second display name policy must ignore",
            },
            "amount": "48.0600",
            "currency": "usd",
            "estimated_days": "3",
            "arrives_by": "20:00:00",
        })

        self.assertTrue(normalized.is_selectable)
        self.assertEqual(normalized.rate_id, "shippo-rate-1")
        self.assertEqual(normalized.carrier_id, "fedex-account-1")
        self.assertEqual(normalized.service_id, "fedex_ground")
        self.assertEqual(normalized.amount, Decimal("48.0600"))
        self.assertEqual(normalized.currency, "USD")
        self.assertEqual(normalized.estimated_days, 3)
        self.assertEqual(normalized.arrives_by_time.hour, 20)
        self.assertEqual(normalized.cost_source, "amount")

    def test_shippo_display_name_cannot_substitute_for_missing_token(self):
        normalized = rate_policy.normalize_shippo_rate({
            "object_id": "shippo-rate-2",
            "provider": "FedEx",
            "servicelevel": {"name": "Ground Home Whatever"},
            "amount": "10.00",
            "currency": "USD",
        })

        self.assertFalse(normalized.is_selectable)
        self.assertIn("missing_service_id", normalized.normalization_errors)
        self.assertIn("missing_carrier_id", normalized.normalization_errors)

    def test_malformed_external_values_are_rejected_without_raising(self):
        normalized = rate_policy.normalize_shippo_rate({
            "object_id": "bad-rate",
            "servicelevel": {"token": "usps_ground_advantage"},
            "amount": "NaN",
            "currency": "dollars",
            "estimated_days": "two",
            "arrives_by": "eventually",
        })

        self.assertFalse(normalized.is_selectable)
        self.assertIn("invalid_amount", normalized.normalization_errors)
        self.assertIn("invalid_currency", normalized.normalization_errors)
        self.assertEqual(
            normalized.warnings,
            ("invalid_estimated_days", "invalid_arrives_by"),
        )

    def test_non_mapping_carrier_payloads_become_explicit_invalid_rates(self):
        shippo = rate_policy.normalize_shippo_rate(None)
        amazon = rate_policy.normalize_amazon_rate(["not", "a", "mapping"])

        self.assertEqual(shippo.normalization_errors, ("invalid_rate_payload",))
        self.assertEqual(amazon.normalization_errors, ("invalid_rate_payload",))
        self.assertFalse(shippo.is_selectable)
        self.assertFalse(amazon.is_selectable)

    def test_amazon_prefers_adjusted_charge_and_delivery_promise(self):
        normalized = rate_policy.normalize_amazon_rate({
            "rateId": "amazon-rate-1",
            "carrierId": "UPS",
            "carrierName": "UPS display label",
            "serviceId": "ups-ground-id",
            "serviceName": "UPS Ground display label",
            "totalCharge": {"value": 50, "unit": "USD"},
            "totalChargeWithAdjustments": {"value": "42.25", "unit": "USD"},
            "promise": {
                "deliveryWindow": {
                    "start": "2026-08-20T08:00:00-05:00",
                    "end": "2026-08-20T20:00:00-05:00",
                }
            },
        })

        self.assertTrue(normalized.is_selectable)
        self.assertEqual(normalized.amount, Decimal("42.25"))
        self.assertEqual(normalized.cost_source, "totalChargeWithAdjustments")
        self.assertEqual(
            normalized.arrives_by,
            datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc),
        )

    def test_amazon_accepts_delivery_window_end_time(self):
        normalized = rate_policy.normalize_amazon_rate({
            "rateId": "amazon-rate-end-time",
            "carrierId": "USPS",
            "serviceId": "usps-ground-advantage",
            "totalCharge": {"value": "14.50", "unit": "USD"},
            "promise": {
                "deliveryWindow": {
                    "endTime": "2026-08-22T19:30:00Z",
                }
            },
        })

        self.assertTrue(normalized.is_selectable)
        self.assertEqual(
            normalized.arrives_by,
            datetime(2026, 8, 22, 19, 30, tzinfo=timezone.utc),
        )

    def test_amazon_response_preserves_ineligible_reason_codes(self):
        normalized = rate_policy.normalize_amazon_rates({
            "payload": {
                "rates": [],
                "ineligibleRates": [{
                    "carrierId": "USPS",
                    "carrierName": "USPS",
                    "serviceId": "usps-ground",
                    "serviceName": "USPS Ground Advantage",
                    "ineligibilityReasons": [{
                        "code": "LATE_DELIVERY",
                        "message": "The promise misses the order deadline.",
                    }],
                }],
            }
        })

        self.assertEqual(len(normalized), 1)
        self.assertFalse(normalized[0].eligible)
        self.assertIn(
            "amazon_ineligible:LATE_DELIVERY",
            normalized[0].upstream_reasons,
        )


class RateSelectionTest(unittest.TestCase):
    def amazon_rate(self, rate_id, amount, delivery_end, **overrides):
        raw = {
            "rateId": rate_id,
            "carrierId": overrides.pop("carrierId", "carrier-1"),
            "serviceId": overrides.pop("serviceId", "service-1"),
            "carrierName": overrides.pop("carrierName", "Ignored carrier label"),
            "serviceName": overrides.pop("serviceName", "Ignored service label"),
            "totalCharge": {"value": amount, "unit": overrides.pop("currency", "USD")},
            "promise": {"deliveryWindow": {"end": delivery_end}},
        }
        raw.update(overrides)
        return rate_policy.normalize_amazon_rate(raw)

    def test_selects_lowest_cost_offer_that_meets_deadline(self):
        late_cheap = self.amazon_rate(
            "late-cheap", "48.06", "2026-08-23T20:00:00Z", serviceId="ground-a"
        )
        on_time = self.amazon_rate(
            "on-time", "52.00", "2026-08-22T20:00:00Z", serviceId="ground-b"
        )
        expensive = self.amazon_rate(
            "expensive", "95.94", "2026-08-21T20:00:00Z", serviceId="ground-c"
        )

        result = rate_policy.select_best_rate(
            [late_cheap, expensive, on_time],
            latest_delivery="2026-08-22T23:59:59Z",
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.selected.rate_id, "on-time")
        self.assertEqual(
            result.reason,
            "selected_lowest_effective_cost_satisfying_policy",
        )
        self.assertEqual(result.cheapest_amount, Decimal("48.06"))
        late_rejection = next(r for r in result.rejections if r.rate_id == "late-cheap")
        self.assertEqual(late_rejection.reasons, ("delivery_after_deadline",))

    def test_order_43359_regression_selects_fedex_by_token_eta_and_cost(self):
        fedex = rate_policy.normalize_shippo_rate({
            "object_id": "fedex-48-06",
            "carrier_account": "fedex-account",
            "provider": "FedEx",
            "servicelevel": {
                "token": "fedex_ground",
                "name": "Home Delivery®",
            },
            "amount": "48.06",
            "currency": "USD",
            "estimated_days": 4,
        })
        usps = rate_policy.normalize_shippo_rate({
            "object_id": "usps-95-94",
            "carrier_account": "usps-account",
            "provider": "USPS",
            "servicelevel": {
                "token": "usps_ground_advantage",
                "name": "Ground Advantage",
            },
            "amount": "95.94",
            "currency": "USD",
            "estimated_days": 5,
        })

        result = rate_policy.select_best_rate(
            [usps, fedex],
            currency="USD",
            ship_at="2026-08-16T18:11:43Z",
            latest_delivery="2026-08-25T06:59:59Z",
            max_over_cheapest="25",
            max_over_cheapest_percent="50",
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.selected.rate_id, "fedex-48-06")
        self.assertEqual(result.selected.service_id, "fedex_ground")

    def test_cost_guard_holds_an_expensive_deadline_match(self):
        late_cheap = self.amazon_rate(
            "late-cheap", "48.06", "2026-08-23T20:00:00Z"
        )
        on_time_expensive = self.amazon_rate(
            "on-time-expensive", "95.94", "2026-08-22T20:00:00Z"
        )

        result = rate_policy.select_best_rate(
            [late_cheap, on_time_expensive],
            latest_delivery="2026-08-22T23:59:59Z",
            max_over_cheapest="10.00",
            max_over_cheapest_percent="25",
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "cost_guard_exceeded")
        self.assertIsNone(result.selected)
        self.assertEqual(result.candidate.rate_id, "on-time-expensive")
        self.assertEqual(result.over_cheapest, Decimal("47.88"))
        candidate_rejection = next(
            rejection
            for rejection in result.rejections
            if rejection.rate_id == "on-time-expensive"
        )
        self.assertEqual(
            candidate_rejection.reasons,
            (
                "max_over_cheapest_exceeded",
                "max_over_cheapest_percent_exceeded",
            ),
        )

    def test_exact_service_id_filter_never_reads_display_names(self):
        allowed = self.amazon_rate(
            "allowed",
            "20.00",
            "2026-08-20T20:00:00Z",
            serviceId="STRUCTURED_STANDARD",
            serviceName="Looks nothing like standard",
        )
        disallowed = self.amazon_rate(
            "disallowed",
            "10.00",
            "2026-08-20T20:00:00Z",
            serviceId="STRUCTURED_EXPRESS",
            serviceName="Standard Ground Home Delivery",
        )

        result = rate_policy.select_best_rate(
            [disallowed, allowed],
            allowed_service_ids={"STRUCTURED_STANDARD"},
        )

        self.assertEqual(result.selected.rate_id, "allowed")
        disallowed_rejection = next(
            rejection for rejection in result.rejections if rejection.rate_id == "disallowed"
        )
        self.assertEqual(disallowed_rejection.reasons, ("service_id_not_allowed",))

    def test_shippo_estimated_days_can_be_projected_from_ship_time(self):
        two_day = rate_policy.normalize_shippo_rate({
            "object_id": "two-day",
            "carrier_account": "carrier-account",
            "servicelevel": {"token": "structured-service-token"},
            "amount": "12.00",
            "currency": "USD",
            "estimated_days": 2,
            "arrives_by": "17:00:00",
        })

        result = rate_policy.select_best_rate(
            [two_day],
            ship_at="2026-08-18T09:00:00-05:00",
            latest_delivery="2026-08-20T23:59:59-05:00",
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.selected.rate_id, "two-day")

    def test_shippo_estimated_days_skip_weekends(self):
        two_day = rate_policy.normalize_shippo_rate({
            "object_id": "friday-two-day",
            "carrier_account": "carrier-account",
            "servicelevel": {"token": "structured-service-token"},
            "amount": "12.00",
            "currency": "USD",
            "estimated_days": 2,
            "arrives_by": "17:00:00",
        })

        monday_deadline = rate_policy.select_best_rate(
            [two_day],
            ship_at="2026-08-21T09:00:00-05:00",
            latest_delivery="2026-08-24T23:59:59-05:00",
        )
        tuesday_deadline = rate_policy.select_best_rate(
            [two_day],
            ship_at="2026-08-21T09:00:00-05:00",
            latest_delivery="2026-08-25T23:59:59-05:00",
        )

        self.assertFalse(monday_deadline.approved)
        self.assertTrue(tuesday_deadline.approved)

    def test_mixed_currencies_require_an_explicit_policy_currency(self):
        usd = self.amazon_rate("usd", "10.00", "2026-08-20T20:00:00Z")
        cad = self.amazon_rate(
            "cad", "9.00", "2026-08-20T20:00:00Z", currency="CAD"
        )

        held = rate_policy.select_best_rate([usd, cad])
        selected = rate_policy.select_best_rate([usd, cad], currency="USD")

        self.assertEqual(held.reason, "multiple_currencies_require_policy_currency")
        self.assertFalse(held.approved)
        self.assertEqual(selected.selected.rate_id, "usd")

    def test_invalid_deadline_holds_instead_of_raising(self):
        rate = self.amazon_rate("valid", "10.00", "2026-08-20T20:00:00Z")

        result = rate_policy.select_best_rate(
            [rate],
            latest_delivery="not-a-date",
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "invalid_latest_delivery")


if __name__ == "__main__":
    unittest.main()
