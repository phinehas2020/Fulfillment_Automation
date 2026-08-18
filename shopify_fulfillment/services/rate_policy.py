"""Pure rate normalization and policy-based shipping-rate selection.

This module deliberately does not import Odoo or any carrier SDK.  Carrier and
service display names are retained only for audit/display purposes; selection
uses opaque rate, carrier, and service identifiers plus price and delivery
data.

The public workflow is:

1. Normalize a carrier response with :func:`normalize_shippo_rates` or
   :func:`normalize_amazon_rates`.
2. Pass the resulting :class:`NormalizedRate` objects to
   :func:`select_best_rate`.
3. Purchase only ``result.selected``.  A ``None`` selection is an intentional
   hold and ``result.reason`` / ``result.rejections`` explain why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


UTC = timezone.utc
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class NormalizedRate:
    """Carrier-neutral rate data used by the selector.

    ``service_name`` and ``carrier_name`` are informational.  Policy matching
    is performed only against ``service_id`` and ``carrier_id``.
    ``amount`` is the effective amount: Shippo's ``amount`` or Amazon's
    ``totalChargeWithAdjustments`` when present and valid, otherwise Amazon's
    ``totalCharge``.
    """

    source: str
    rate_id: Optional[str]
    service_id: Optional[str]
    carrier_id: Optional[str]
    amount: Optional[Decimal]
    currency: Optional[str]
    estimated_days: Optional[int] = None
    arrives_by: Optional[datetime] = None
    arrives_by_time: Optional[time] = None
    service_name: Optional[str] = None
    carrier_name: Optional[str] = None
    cost_source: Optional[str] = None
    eligible: bool = True
    normalization_errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    upstream_reasons: Tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_selectable(self) -> bool:
        """Whether the offer is structurally safe and upstream-eligible."""

        return self.eligible and not self.normalization_errors


@dataclass(frozen=True)
class RateRejection:
    """Machine-readable reasons an offer was not selected."""

    source: str
    rate_id: Optional[str]
    service_id: Optional[str]
    carrier_id: Optional[str]
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class RateSelection:
    """Result of applying a rate policy.

    ``selected`` is non-null only when the purchase is approved.  ``candidate``
    retains the cheapest policy-matching offer when a cost guard holds it for
    review.  ``cheapest_amount`` is the cheapest structurally valid,
    upstream-eligible offer before deadline and allow-list filters, in the
    selected currency.
    """

    selected: Optional[NormalizedRate]
    reason: str
    rejections: Tuple[RateRejection, ...]
    currency: Optional[str] = None
    cheapest_amount: Optional[Decimal] = None
    candidate: Optional[NormalizedRate] = None
    over_cheapest: Optional[Decimal] = None

    @property
    def approved(self) -> bool:
        return self.selected is not None


def normalize_shippo_rate(rate: Any) -> NormalizedRate:
    """Normalize one Shippo rate without raising on malformed carrier data."""

    if not isinstance(rate, Mapping):
        return _invalid_payload("shippo", rate)

    errors = []
    warnings = []

    rate_id = _identifier(rate.get("object_id"))
    if rate_id is None:
        errors.append("missing_rate_id")

    servicelevel = rate.get("servicelevel")
    if not isinstance(servicelevel, Mapping):
        servicelevel = {}
    service_id = _identifier(servicelevel.get("token"))
    if service_id is None:
        errors.append("missing_service_id")

    # carrier_account is Shippo's stable account identifier.  provider is a
    # display label and must never drive selection.
    carrier_id = _identifier(rate.get("carrier_account"))
    if carrier_id is None:
        errors.append("missing_carrier_id")
    amount = _parse_nonnegative_decimal(rate.get("amount"))
    if amount is None:
        errors.append("invalid_amount")

    currency = _currency(rate.get("currency"))
    if currency is None:
        errors.append(
            "missing_currency" if _is_blank(rate.get("currency")) else "invalid_currency"
        )

    estimated_days = _parse_nonnegative_int(rate.get("estimated_days"))
    if rate.get("estimated_days") is not None and estimated_days is None:
        warnings.append("invalid_estimated_days")

    arrives_by, arrives_by_time = _parse_arrival(rate.get("arrives_by"))
    if rate.get("arrives_by") and arrives_by is None and arrives_by_time is None:
        warnings.append("invalid_arrives_by")

    return NormalizedRate(
        source="shippo",
        rate_id=rate_id,
        service_id=service_id,
        carrier_id=carrier_id,
        amount=amount,
        currency=currency,
        estimated_days=estimated_days,
        arrives_by=arrives_by,
        arrives_by_time=arrives_by_time,
        service_name=_display_text(servicelevel.get("name")),
        carrier_name=_display_text(rate.get("provider")),
        cost_source="amount" if amount is not None else None,
        normalization_errors=tuple(errors),
        warnings=tuple(warnings),
        raw=rate,
    )


def normalize_shippo_rates(response_or_rates: Any) -> Tuple[NormalizedRate, ...]:
    """Normalize Shippo shipment or paginated-rate output.

    Accepted inputs are a direct sequence, a shipment response containing
    ``rates``, or a paginated response containing ``results``.  Invalid
    container shapes produce an empty tuple rather than an exception.
    """

    values = response_or_rates
    if isinstance(values, Mapping):
        if isinstance(values.get("rates"), Sequence) and not isinstance(
            values.get("rates"), (str, bytes, bytearray)
        ):
            values = values.get("rates")
        elif isinstance(values.get("results"), Sequence) and not isinstance(
            values.get("results"), (str, bytes, bytearray)
        ):
            values = values.get("results")
        else:
            return ()

    if not isinstance(values, Iterable) or isinstance(
        values, (str, bytes, bytearray, Mapping)
    ):
        return ()
    return tuple(normalize_shippo_rate(rate) for rate in values)


def normalize_amazon_rate(rate: Any, *, eligible: bool = True) -> NormalizedRate:
    """Normalize one Amazon Shipping v2 eligible or ineligible rate."""

    if not isinstance(rate, Mapping):
        return _invalid_payload("amazon_shipping_v2", rate, eligible=eligible)

    errors = []
    warnings = []
    upstream_reasons = _amazon_ineligibility_reasons(rate) if not eligible else ()

    rate_id = _identifier(rate.get("rateId"))
    service_id = _identifier(rate.get("serviceId"))
    carrier_id = _identifier(rate.get("carrierId"))
    if rate_id is None:
        errors.append("missing_rate_id")
    if service_id is None:
        errors.append("missing_service_id")
    if carrier_id is None:
        errors.append("missing_carrier_id")

    amount, currency, cost_source = _amazon_effective_charge(rate)
    if amount is None:
        errors.append("invalid_amount")
    if currency is None:
        errors.append("missing_or_invalid_currency")

    promise = rate.get("promise")
    delivery_window = promise.get("deliveryWindow") if isinstance(promise, Mapping) else None
    arrives_by = None
    if isinstance(delivery_window, Mapping):
        delivery_end = delivery_window.get("end")
        if delivery_end is None:
            delivery_end = delivery_window.get("endTime")
        arrives_by = _parse_datetime(delivery_end, date_as_end=True)
        if delivery_end and arrives_by is None:
            warnings.append("invalid_delivery_promise_end")
    elif promise is not None:
        warnings.append("invalid_delivery_promise")

    return NormalizedRate(
        source="amazon_shipping_v2",
        rate_id=rate_id,
        service_id=service_id,
        carrier_id=carrier_id,
        amount=amount,
        currency=currency,
        arrives_by=arrives_by,
        service_name=_display_text(rate.get("serviceName")),
        carrier_name=_display_text(rate.get("carrierName")),
        cost_source=cost_source,
        eligible=eligible,
        normalization_errors=tuple(errors),
        warnings=tuple(warnings),
        upstream_reasons=upstream_reasons,
        raw=rate,
    )


def normalize_amazon_rates(response_or_rates: Any) -> Tuple[NormalizedRate, ...]:
    """Normalize an Amazon Shipping v2 ``getRates`` response.

    The official response wraps ``rates`` and ``ineligibleRates`` in
    ``payload``.  A direct rate sequence is also accepted for callers that
    have already unwrapped the response.
    """

    if isinstance(response_or_rates, Mapping):
        payload = response_or_rates.get("payload", response_or_rates)
        if not isinstance(payload, Mapping):
            return ()

        eligible_rates = _sequence_or_empty(payload.get("rates"))
        ineligible_rates = _sequence_or_empty(payload.get("ineligibleRates"))
        return tuple(
            [normalize_amazon_rate(rate, eligible=True) for rate in eligible_rates]
            + [normalize_amazon_rate(rate, eligible=False) for rate in ineligible_rates]
        )

    values = _sequence_or_empty(response_or_rates)
    return tuple(normalize_amazon_rate(rate) for rate in values)


def select_best_rate(
    rates: Iterable[NormalizedRate],
    *,
    currency: Optional[str] = None,
    allowed_rate_ids: Optional[Iterable[str]] = None,
    allowed_carrier_ids: Optional[Iterable[str]] = None,
    allowed_service_ids: Optional[Iterable[str]] = None,
    latest_delivery: Any = None,
    ship_at: Any = None,
    max_estimated_days: Any = None,
    max_over_cheapest: Any = None,
    max_over_cheapest_percent: Any = None,
) -> RateSelection:
    """Choose the lowest effective-cost rate satisfying structured policy.

    All allow lists use exact, case-sensitive opaque IDs.  Display names are
    never examined.

    ``latest_delivery`` may be an ISO-8601 string, ``date``, or ``datetime``.
    Date-only values mean the end of that UTC day; timezone-less datetimes are
    treated as UTC.  Amazon promise ends and full Shippo ``arrives_by`` values
    can be compared directly.  A Shippo estimate containing only
    ``estimated_days`` can be projected only when ``ship_at`` is provided.

    The optional over-cheapest guards compare the selected policy candidate to
    the cheapest structurally valid, upstream-eligible rate in the same
    currency *before* deadline and allow-list filters.  If either configured
    guard is exceeded, no purchase is approved.  Percent values use percentage
    points (``50`` means fifty percent), and when both guards are set the more
    restrictive outcome wins.
    """

    normalized_rates = tuple(rates or ())
    if not normalized_rates:
        return RateSelection(None, "no_rates", ())
    if any(not isinstance(rate, NormalizedRate) for rate in normalized_rates):
        return RateSelection(None, "invalid_normalized_rate_input", ())

    explicit_currency = None
    if currency is not None:
        explicit_currency = _currency(currency)
        if explicit_currency is None:
            return RateSelection(None, "invalid_policy_currency", ())

    deadline = None
    if latest_delivery is not None:
        deadline = _parse_datetime(latest_delivery, date_as_end=True)
        if deadline is None:
            return RateSelection(None, "invalid_latest_delivery", ())

    shipment_time = None
    if ship_at is not None:
        shipment_time = _parse_datetime(ship_at, date_as_end=False)
        if shipment_time is None:
            return RateSelection(None, "invalid_ship_at", ())

    max_days = None
    if max_estimated_days is not None:
        max_days = _parse_nonnegative_int(max_estimated_days)
        if max_days is None:
            return RateSelection(None, "invalid_max_estimated_days", ())

    absolute_guard = None
    if max_over_cheapest is not None:
        absolute_guard = _parse_nonnegative_decimal(max_over_cheapest)
        if absolute_guard is None:
            return RateSelection(None, "invalid_max_over_cheapest", ())

    percent_guard = None
    if max_over_cheapest_percent is not None:
        percent_guard = _parse_nonnegative_decimal(max_over_cheapest_percent)
        if percent_guard is None:
            return RateSelection(None, "invalid_max_over_cheapest_percent", ())

    rate_ids, invalid = _identifier_allowlist(allowed_rate_ids)
    if invalid:
        return RateSelection(None, "invalid_allowed_rate_ids", ())
    carrier_ids, invalid = _identifier_allowlist(allowed_carrier_ids)
    if invalid:
        return RateSelection(None, "invalid_allowed_carrier_ids", ())
    service_ids, invalid = _identifier_allowlist(allowed_service_ids)
    if invalid:
        return RateSelection(None, "invalid_allowed_service_ids", ())

    rejections = []
    structurally_valid = []
    for rate in normalized_rates:
        reasons = list(rate.normalization_errors)
        if not rate.eligible:
            reasons.append("upstream_ineligible")
            reasons.extend(rate.upstream_reasons)
        if reasons:
            rejections.append(_rejection(rate, reasons))
        else:
            structurally_valid.append(rate)

    if not structurally_valid:
        return RateSelection(None, "no_structurally_valid_eligible_rates", tuple(rejections))

    available_currencies = {rate.currency for rate in structurally_valid}
    if explicit_currency is None:
        if len(available_currencies) != 1:
            rejections.extend(
                _rejection(rate, ("multiple_currencies_require_policy_currency",))
                for rate in structurally_valid
            )
            return RateSelection(
                None,
                "multiple_currencies_require_policy_currency",
                tuple(rejections),
            )
        selected_currency = next(iter(available_currencies))
    else:
        selected_currency = explicit_currency

    comparable = []
    for rate in structurally_valid:
        if rate.currency != selected_currency:
            rejections.append(_rejection(rate, ("currency_mismatch",)))
        else:
            comparable.append(rate)

    if not comparable:
        return RateSelection(
            None,
            "no_rates_in_policy_currency",
            tuple(rejections),
            currency=selected_currency,
        )

    cheapest = min(comparable, key=_rate_sort_key)
    candidates = []
    for rate in comparable:
        reasons = []
        if rate_ids is not None and rate.rate_id not in rate_ids:
            reasons.append("rate_id_not_allowed")
        if carrier_ids is not None and rate.carrier_id not in carrier_ids:
            reasons.append("carrier_id_not_allowed")
        if service_ids is not None and rate.service_id not in service_ids:
            reasons.append("service_id_not_allowed")

        if max_days is not None:
            if rate.estimated_days is None:
                reasons.append("missing_estimated_days")
            elif rate.estimated_days > max_days:
                reasons.append("estimated_days_exceed_policy")

        if deadline is not None:
            projected_delivery = _projected_delivery(rate, shipment_time)
            if projected_delivery is None:
                reasons.append("missing_delivery_estimate")
            elif projected_delivery > deadline:
                reasons.append("delivery_after_deadline")

        if reasons:
            rejections.append(_rejection(rate, reasons))
        else:
            candidates.append(rate)

    if not candidates:
        return RateSelection(
            None,
            "no_rates_satisfy_policy",
            tuple(rejections),
            currency=selected_currency,
            cheapest_amount=cheapest.amount,
        )

    candidate = min(candidates, key=_rate_sort_key)
    over_cheapest = candidate.amount - cheapest.amount
    guard_reasons = []
    if absolute_guard is not None and over_cheapest > absolute_guard:
        guard_reasons.append("max_over_cheapest_exceeded")
    if (
        percent_guard is not None
        and _percent_over(cheapest.amount, candidate.amount) > percent_guard
    ):
        guard_reasons.append("max_over_cheapest_percent_exceeded")

    if guard_reasons:
        rejections.append(_rejection(candidate, guard_reasons))
        return RateSelection(
            None,
            "cost_guard_exceeded",
            tuple(rejections),
            currency=selected_currency,
            cheapest_amount=cheapest.amount,
            candidate=candidate,
            over_cheapest=over_cheapest,
        )

    return RateSelection(
        selected=candidate,
        reason="selected_lowest_effective_cost_satisfying_policy",
        rejections=tuple(rejections),
        currency=selected_currency,
        cheapest_amount=cheapest.amount,
        candidate=candidate,
        over_cheapest=over_cheapest,
    )


def _invalid_payload(source: str, raw: Any, *, eligible: bool = False) -> NormalizedRate:
    safe_raw = raw if isinstance(raw, Mapping) else {}
    return NormalizedRate(
        source=source,
        rate_id=None,
        service_id=None,
        carrier_id=None,
        amount=None,
        currency=None,
        eligible=eligible,
        normalization_errors=("invalid_rate_payload",),
        raw=safe_raw,
    )


def _identifier(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _display_text(value: Any) -> Optional[str]:
    return _identifier(value)


def _currency(value: Any) -> Optional[str]:
    value = _identifier(value)
    if value is None:
        return None
    value = value.upper()
    return value if _CURRENCY_RE.fullmatch(value) else None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_nonnegative_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _parse_nonnegative_int(value: Any) -> Optional[int]:
    parsed = _parse_nonnegative_decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    try:
        return int(parsed)
    except (OverflowError, ValueError):
        return None


def _parse_datetime(value: Any, *, date_as_end: bool) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if date_as_end else time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                parsed_date = date.fromisoformat(text)
            except ValueError:
                return None
            parsed = datetime.combine(parsed_date, time.max if date_as_end else time.min)
        else:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_arrival(value: Any) -> Tuple[Optional[datetime], Optional[time]]:
    parsed_datetime = _parse_datetime(value, date_as_end=True)
    if parsed_datetime is not None:
        return parsed_datetime, None
    if not isinstance(value, str):
        return None, None
    text = value.strip()
    if not text:
        return None, None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return None, time.fromisoformat(text)
    except ValueError:
        return None, None


def _amazon_effective_charge(
    rate: Mapping[str, Any],
) -> Tuple[Optional[Decimal], Optional[str], Optional[str]]:
    for field_name in ("totalChargeWithAdjustments", "totalCharge"):
        money = rate.get(field_name)
        if not isinstance(money, Mapping):
            continue
        amount = _parse_nonnegative_decimal(money.get("value"))
        currency = _currency(money.get("unit"))
        if amount is not None and currency is not None:
            return amount, currency, field_name
    return None, None, None


def _amazon_ineligibility_reasons(rate: Mapping[str, Any]) -> Tuple[str, ...]:
    reasons = []
    for item in _sequence_or_empty(rate.get("ineligibilityReasons")):
        if not isinstance(item, Mapping):
            continue
        code = _identifier(item.get("code"))
        message = _display_text(item.get("message"))
        if code:
            reasons.append("amazon_ineligible:%s" % code)
        if message:
            compact_message = " ".join(message.split())[:300]
            reasons.append("amazon_ineligible_message:%s" % compact_message)
    return tuple(reasons)


def _sequence_or_empty(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _identifier_allowlist(
    values: Optional[Iterable[str]],
) -> Tuple[Optional[frozenset[str]], bool]:
    if values is None:
        return None, False
    if isinstance(values, str):
        values = (values,)
    try:
        materialized = tuple(values)
    except TypeError:
        return None, True
    identifiers = []
    for value in materialized:
        normalized = _identifier(value)
        if normalized is None:
            return None, True
        identifiers.append(normalized)
    return frozenset(identifiers), False


def _projected_delivery(
    rate: NormalizedRate, shipment_time: Optional[datetime]
) -> Optional[datetime]:
    if rate.arrives_by is not None:
        return rate.arrives_by
    if rate.estimated_days is None or shipment_time is None:
        return None

    projected_date = shipment_time.date()
    remaining_days = rate.estimated_days
    while remaining_days > 0:
        projected_date += timedelta(days=1)
        if projected_date.weekday() < 5:
            remaining_days -= 1
    projected_time = rate.arrives_by_time or time.max
    if projected_time.tzinfo is None:
        projected_time = projected_time.replace(tzinfo=shipment_time.tzinfo or UTC)
    return datetime.combine(projected_date, projected_time).astimezone(UTC)


def _rate_sort_key(rate: NormalizedRate) -> Tuple[Any, ...]:
    delivery = rate.arrives_by or datetime.max.replace(tzinfo=UTC)
    return (
        rate.amount,
        delivery,
        rate.source,
        rate.carrier_id or "",
        rate.service_id or "",
        rate.rate_id or "",
    )


def _percent_over(cheapest: Decimal, candidate: Decimal) -> Decimal:
    difference = candidate - cheapest
    if cheapest == 0:
        return Decimal(0) if difference <= 0 else Decimal("Infinity")
    return difference * Decimal(100) / cheapest


def _rejection(rate: NormalizedRate, reasons: Iterable[str]) -> RateRejection:
    return RateRejection(
        source=rate.source,
        rate_id=rate.rate_id,
        service_id=rate.service_id,
        carrier_id=rate.carrier_id,
        reasons=tuple(dict.fromkeys(reasons)),
    )


__all__ = [
    "NormalizedRate",
    "RateRejection",
    "RateSelection",
    "normalize_amazon_rate",
    "normalize_amazon_rates",
    "normalize_shippo_rate",
    "normalize_shippo_rates",
    "select_best_rate",
]
