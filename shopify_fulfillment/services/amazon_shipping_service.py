"""Amazon Shipping API v2 adapter for on-Amazon Buy Shipping labels.

The adapter intentionally exposes a Shippo-shaped rate dictionary at the Odoo
boundary.  That keeps the existing shipment/audit pipeline small while stable
Amazon identifiers, promises, and document capabilities remain available under
the private ``_amazon`` key.

Label purchase is never retried automatically.  A timeout after submitting a
purchase is an ambiguous financial state and must be reconciled before another
purchase attempt.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests

from .address_utils import normalize_address_lines


_logger = logging.getLogger(__name__)


def _sanitize_phone(phone: str) -> str:
    if not phone:
        return ""
    cleaned = re.sub(
        r"\s*(?:ext(?:ension)?\.?|x)\s*[:.#-]?\s*\d+.*$",
        "",
        str(phone),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^\d\s\-()+]", "", cleaned)
    return " ".join(cleaned.split()).strip()


class AmazonShippingError(Exception):
    """Base error for Amazon Shipping integration failures."""


class AmazonShippingConfigurationError(AmazonShippingError):
    """Raised when required credentials or order metadata are unavailable."""


class AmazonShippingPurchaseUncertain(AmazonShippingError):
    """Raised when a purchase may have succeeded but no response was received."""

    def __init__(self, message, *, shipment_id=None, response_data=None):
        super().__init__(message)
        self.shipment_id = shipment_id
        self.response_data = response_data or {}


class AmazonShippingService:
    """Minimal LWA + SP-API client for Shipping API v2 in North America."""

    provider_name = "amazon_shipping"
    LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
    DEFAULT_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
    DEFAULT_BUSINESS_ID = "AmazonShipping_US"
    DEFAULT_MARKETPLACE_ID = "ATVPDKIKX0DER"
    USER_AGENT = "HomesteadFulfillment/0.5 (Language=Python; Platform=Odoo18)"
    TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
    POLICY_VERSION = "amazon-buy-shipping-v2-2026-08"

    _token_lock = threading.Lock()
    _token_cache: Dict[str, Dict[str, Any]] = {}

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        endpoint: str = DEFAULT_ENDPOINT,
        business_id: str = DEFAULT_BUSINESS_ID,
        marketplace_id: str = DEFAULT_MARKETPLACE_ID,
        required_vas_preferences: Optional[dict] = None,
    ):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.endpoint = (endpoint or self.DEFAULT_ENDPOINT).rstrip("/")
        self.business_id = business_id or self.DEFAULT_BUSINESS_ID
        self.marketplace_id = marketplace_id or self.DEFAULT_MARKETPLACE_ID
        self.required_vas_preferences = required_vas_preferences or {}
        self._order_items_cache: Dict[str, List[dict]] = {}

        if not all((self.client_id, self.client_secret, self.refresh_token)):
            raise AmazonShippingConfigurationError(
                "Amazon Shipping requires an LWA client ID, client secret, and refresh token."
            )

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls, env, *, require_enabled: bool = True):
        """Build the client from process environment or admin-only Odoo params.

        Process environment wins so production secrets can be injected without
        entering them in the Odoo UI.  No Amazon credentials are exposed through
        the module's general-user configuration wizard.
        """
        params = env["ir.config_parameter"].sudo()
        enabled = cls._truthy(
            os.environ.get("AMAZON_SHIPPING_ENABLED")
            or params.get_param("amazon_shipping.enabled", "False")
        )
        if require_enabled and not enabled:
            return None

        def _value(env_key: str, param_key: str, default: str = "") -> str:
            return (
                os.environ.get(env_key)
                or params.get_param(param_key, default)
                or default
            )

        client_id = _value("AMAZON_LWA_CLIENT_ID", "amazon_shipping.lwa_client_id")
        client_secret = _value(
            "AMAZON_LWA_CLIENT_SECRET", "amazon_shipping.lwa_client_secret"
        )
        refresh_token = _value(
            "AMAZON_LWA_REFRESH_TOKEN", "amazon_shipping.refresh_token"
        )
        if not all((client_id, client_secret, refresh_token)):
            return None

        raw_vas_preferences = _value(
            "AMAZON_REQUIRED_VAS_PREFERENCES",
            "amazon_shipping.required_vas_preferences",
            "{}",
        )
        try:
            vas_preferences = json.loads(raw_vas_preferences)
        except (TypeError, ValueError):
            vas_preferences = {}
        if not isinstance(vas_preferences, dict):
            vas_preferences = {}

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            endpoint=_value(
                "AMAZON_SP_API_ENDPOINT",
                "amazon_shipping.endpoint",
                cls.DEFAULT_ENDPOINT,
            ),
            business_id=_value(
                "AMAZON_SHIPPING_BUSINESS_ID",
                "amazon_shipping.business_id",
                cls.DEFAULT_BUSINESS_ID,
            ),
            marketplace_id=_value(
                "AMAZON_MARKETPLACE_ID",
                "amazon_shipping.marketplace_id",
                cls.DEFAULT_MARKETPLACE_ID,
            ),
            required_vas_preferences=vas_preferences,
        )

    @classmethod
    def credentials_configured(cls, env) -> bool:
        return cls.from_env(env, require_enabled=False) is not None

    def _access_token(self) -> str:
        cache_key = f"{self.client_id}:{hash(self.refresh_token)}"
        now = time.time()
        cached = self._token_cache.get(cache_key)
        if cached and cached.get("expires_at", 0) > now + 60:
            return cached["access_token"]

        with self._token_lock:
            cached = self._token_cache.get(cache_key)
            if cached and cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"]

            try:
                response = requests.post(
                    self.LWA_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=15,
                )
            except requests.RequestException as exc:
                raise AmazonShippingError(
                    "Could not reach Login with Amazon for an access token."
                ) from exc

            if response.status_code >= 400:
                raise AmazonShippingError(
                    "Login with Amazon rejected the configured credentials "
                    f"(HTTP {response.status_code})."
                )

            try:
                payload = response.json()
                access_token = payload["access_token"]
                expires_in = int(payload.get("expires_in") or 3600)
            except (KeyError, TypeError, ValueError) as exc:
                raise AmazonShippingError(
                    "Login with Amazon returned an invalid token response."
                ) from exc

            self._token_cache[cache_key] = {
                "access_token": access_token,
                "expires_at": time.time() + max(expires_in, 120),
            }
            return access_token

    @staticmethod
    def _error_summary(response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if not isinstance(errors, list):
            return f"HTTP {response.status_code}"
        parts = []
        for error in errors[:3]:
            if not isinstance(error, dict):
                continue
            code = str(error.get("code") or "error")
            message = str(error.get("message") or "request rejected")
            parts.append(f"{code}: {message[:240]}")
        return "; ".join(parts) or f"HTTP {response.status_code}"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "host": self.endpoint.split("//", 1)[-1],
            "user-agent": self.USER_AGENT,
            "x-amz-access-token": self._access_token(),
            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "x-amzn-shipping-business-id": self.business_id,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_read: bool = False,
        purchase: bool = False,
    ) -> dict:
        attempts = 3 if retry_read else 1
        url = f"{self.endpoint}{path}"

        for attempt in range(1, attempts + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_payload,
                    params=params,
                    timeout=25,
                )
            except requests.RequestException as exc:
                if purchase:
                    raise AmazonShippingPurchaseUncertain(
                        "Amazon label purchase returned no response. Do not retry until "
                        "the purchase is reconciled in Amazon."
                    ) from exc
                if attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise AmazonShippingError(
                    "Amazon Shipping API could not be reached."
                ) from exc

            if response.status_code < 400:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    parsed_response = response.json()
                except ValueError as exc:
                    if purchase:
                        raise AmazonShippingPurchaseUncertain(
                            "Amazon returned an unreadable success response after a mutation."
                        ) from exc
                    raise AmazonShippingError(
                        "Amazon Shipping returned a non-JSON response."
                    ) from exc
                if not isinstance(parsed_response, dict):
                    if purchase:
                        raise AmazonShippingPurchaseUncertain(
                            "Amazon returned a malformed success response after a mutation.",
                            response_data=self._safe_response_data(parsed_response),
                        )
                    raise AmazonShippingError(
                        "Amazon Shipping returned an invalid response object."
                    )
                return parsed_response

            if purchase and response.status_code in self.TRANSIENT_STATUS_CODES:
                raise AmazonShippingPurchaseUncertain(
                    "Amazon returned a transient response after a mutation request. "
                    "Do not retry until the shipment state is reconciled in Amazon."
                )

            if (
                response.status_code in self.TRANSIENT_STATUS_CODES
                and attempt < attempts
            ):
                time.sleep(attempt)
                continue

            raise AmazonShippingError(
                "Amazon Shipping request failed: " + self._error_summary(response)
            )

        raise AmazonShippingError("Amazon Shipping request exhausted all attempts.")

    def get_order_items(self, amazon_order_id: str) -> List[dict]:
        amazon_order_id = (amazon_order_id or "").strip()
        if not amazon_order_id:
            raise AmazonShippingConfigurationError(
                "Amazon order ID is missing from the marketplace order payload."
            )
        if amazon_order_id in self._order_items_cache:
            return self._order_items_cache[amazon_order_id]

        path = f"/orders/v0/orders/{quote(amazon_order_id, safe='')}/orderItems"
        items = []
        next_token = None
        for _page in range(20):
            data = self._request(
                "GET",
                path,
                params={"NextToken": next_token} if next_token else None,
                retry_read=True,
            )
            payload = data.get("payload") or {}
            items.extend(payload.get("OrderItems") or [])
            next_token = payload.get("NextToken")
            if not next_token:
                break
        if not items:
            raise AmazonShippingError(
                f"Amazon returned no order items for {amazon_order_id}."
            )
        self._order_items_cache[amazon_order_id] = items
        return items

    @staticmethod
    def _currency(value: Any, default: str = "USD") -> str:
        return str(value or default).upper()

    @staticmethod
    def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default

    @staticmethod
    def _line_asin(order_line) -> str:
        asin = getattr(order_line, "amazon_asin", None)
        if asin:
            return str(asin).strip()
        return ""

    def _match_order_item(self, order_line, amazon_items: Iterable[dict]) -> dict:
        sku = (order_line.sku or "").strip().lower()
        asin = self._line_asin(order_line).lower()
        candidates = []
        for item in amazon_items:
            seller_sku = str(item.get("SellerSKU") or "").strip().lower()
            item_asin = str(item.get("ASIN") or "").strip().lower()
            if sku and seller_sku == sku:
                candidates.append(item)
            elif asin and item_asin == asin:
                candidates.append(item)

        if len(candidates) != 1:
            raise AmazonShippingError(
                "Could not uniquely match Amazon OrderItemId for fulfillment line "
                f"{order_line.sku or order_line.id}."
            )
        return candidates[0]

    @staticmethod
    def _company_address(company) -> dict:
        street1, street2 = normalize_address_lines(company.street, company.street2)
        address = {
            "name": company.name or "Homestead Gristmill",
            "companyName": company.name or "Homestead Gristmill",
            "addressLine1": street1 or "",
            "city": company.city or "",
            "stateOrRegion": company.state_id.code if company.state_id else "",
            "postalCode": company.zip or "",
            "countryCode": company.country_id.code if company.country_id else "US",
            "email": company.email or "no-reply@homesteadgristmill.com",
            "phoneNumber": _sanitize_phone(company.phone or ""),
        }
        if street2:
            address["addressLine2"] = street2
        return address

    def _package_items(self, order, packed_box, amazon_items: List[dict]) -> List[dict]:
        items = []
        quantities = packed_box.line_quantities
        for line in order.line_ids.filtered(lambda rec: rec.id in packed_box.line_ids):
            quantity = int(quantities.get(line.id) or 0)
            if quantity <= 0:
                continue
            amazon_item = self._match_order_item(line, amazon_items)
            order_item_id = str(amazon_item.get("OrderItemId") or "").strip()
            if not order_item_id:
                raise AmazonShippingError(
                    f"Amazon order item ID is missing for line {line.sku or line.id}."
                )

            if hasattr(line, "amazon_order_item_id") and not line.amazon_order_item_id:
                line.sudo().write({"amazon_order_item_id": order_item_id})

            ordered_qty = int(amazon_item.get("QuantityOrdered") or quantity or 1)
            total_price = self._decimal(
                (amazon_item.get("ItemPrice") or {}).get("Amount"),
                self._decimal(getattr(line, "unit_price", 0)) * ordered_qty,
            )
            unit_price = total_price / max(ordered_qty, 1)
            currency = self._currency(
                (amazon_item.get("ItemPrice") or {}).get("CurrencyCode"),
                getattr(order, "order_currency", None) or "USD",
            )
            items.append(
                {
                    "itemValue": {"unit": currency, "value": float(unit_price)},
                    "description": (line.title or line.sku or "Item")[:256],
                    "itemIdentifier": order_item_id,
                    "quantity": quantity,
                    "weight": {
                        "unit": "GRAM",
                        "value": float(line.weight or 0),
                    },
                    "isHazmat": False,
                }
            )
        if not items:
            raise AmazonShippingError("No Amazon order items were assigned to this box.")
        return items

    @staticmethod
    def _zpl_document_spec(rate: dict) -> Optional[dict]:
        for spec in rate.get("supportedDocumentSpecifications") or []:
            if str(spec.get("format") or "").upper() != "ZPL":
                continue
            size = spec.get("size") or {}
            try:
                width = float(size.get("width"))
                length = float(size.get("length"))
            except (TypeError, ValueError):
                continue
            dimensions = sorted((round(width, 2), round(length, 2)))
            if str(size.get("unit") or "").upper() != "INCH" or dimensions != [4.0, 6.0]:
                continue
            for option in spec.get("printOptions") or []:
                dpis = option.get("supportedDPIs") or []
                dpi = 203 if 203 in dpis else (dpis[0] if dpis else None)
                layouts = option.get("supportedPageLayouts") or []
                joins = option.get("supportedFileJoiningOptions") or [False]
                document_types = [
                    detail.get("name")
                    for detail in option.get("supportedDocumentDetails") or []
                    if detail.get("name")
                ]
                if "LABEL" not in document_types:
                    continue
                requested = {
                    "format": "ZPL",
                    "size": {
                        "width": size.get("width") or 4,
                        "length": size.get("length") or 6,
                        "unit": size.get("unit") or "INCH",
                    },
                    "pageLayout": layouts[0] if layouts else "DEFAULT",
                    "needFileJoining": False if False in joins else bool(joins[0]),
                    "requestedDocumentTypes": ["LABEL"],
                }
                if dpi:
                    requested["dpi"] = dpi
                return requested
        return None

    def _required_value_added_services(self, rate: dict):
        selected = []
        unresolved_groups = []
        for group in rate.get("availableValueAddedServiceGroups") or []:
            if not group.get("isRequired"):
                continue
            group_id = str(group.get("groupId") or "").strip()
            option_ids = [
                str(option.get("id"))
                for option in (group.get("valueAddedServices") or [])
                if option.get("id")
            ]
            preferred = self.required_vas_preferences.get(group_id)
            if preferred in option_ids:
                selected.append({"id": preferred})
            elif len(option_ids) == 1:
                selected.append({"id": option_ids[0]})
            else:
                unresolved_groups.append(group_id or "unknown_required_vas_group")
        return selected, unresolved_groups

    def _legacy_rate(self, rate: dict, request_token: str) -> dict:
        adjusted = rate.get("totalChargeWithAdjustments") or rate.get("totalCharge") or {}
        promise = rate.get("promise") or {}
        delivery = promise.get("deliveryWindow") or {}
        document_spec = self._zpl_document_spec(rate)
        requested_vas, unresolved_vas = self._required_value_added_services(rate)
        return {
            "object_id": rate.get("rateId"),
            "provider": rate.get("carrierName") or rate.get("carrierId") or "Amazon",
            "servicelevel": {
                "name": rate.get("serviceName") or rate.get("serviceId") or "",
                "token": rate.get("serviceId") or "",
            },
            "amount": str(adjusted.get("value") or "0"),
            "currency": adjusted.get("unit") or "USD",
            "estimated_days": None,
            "arrives_by": delivery.get("endTime") or delivery.get("end"),
            "attributes": ["AMAZON_ELIGIBLE"],
            "_source": "amazon_shipping",
            "_amazon": {
                "request_token": request_token,
                "rate_id": rate.get("rateId"),
                "carrier_id": rate.get("carrierId"),
                "service_id": rate.get("serviceId"),
                "promise": promise,
                "document_spec": document_spec,
                "requires_additional_inputs": bool(rate.get("requiresAdditionalInputs")),
                "requested_value_added_services": requested_vas,
                "unresolved_required_vas_groups": unresolved_vas,
                "benefits": rate.get("benefits"),
                "raw_rate": rate,
            },
        }

    def get_rates_for_box(
        self,
        *,
        order,
        packed_box,
        box,
        total_weight_grams: float,
        sender_company,
        sequence: int,
    ):
        amazon_order_id = (getattr(order, "amazon_order_id", "") or "").strip()
        if not amazon_order_id:
            raise AmazonShippingConfigurationError(
                "Amazon Order Id is missing; the order was held before rating."
            )

        amazon_items = self.get_order_items(amazon_order_id)
        package_items = self._package_items(order, packed_box, amazon_items)
        currency = getattr(order, "order_currency", None) or "USD"
        insured_value = sum(
            self._decimal(item["itemValue"]["value"]) * int(item["quantity"])
            for item in package_items
        )
        reference = f"{order.order_name or order.id}-box-{sequence}"
        ship_from = self._company_address(sender_company)
        missing_ship_from = [
            label
            for label, key in (
                ("street", "addressLine1"),
                ("city", "city"),
                ("state", "stateOrRegion"),
                ("postal code", "postalCode"),
                ("country", "countryCode"),
                ("phone", "phoneNumber"),
            )
            if not ship_from.get(key)
        ]
        if missing_ship_from:
            raise AmazonShippingConfigurationError(
                "Amazon Shipping sender address is incomplete: "
                + ", ".join(missing_ship_from)
            )

        payload = {
            "shipFrom": ship_from,
            "packages": [
                {
                    "dimensions": {
                        "length": float(box.length or 0),
                        "width": float(box.width or 0),
                        "height": float(box.height or 0),
                        "unit": "INCH",
                    },
                    "weight": {
                        "unit": "GRAM",
                        "value": float(total_weight_grams or 0),
                    },
                    "insuredValue": {
                        "unit": currency,
                        "value": float(insured_value),
                    },
                    "isHazmat": False,
                    "sellerDisplayName": sender_company.name or "Homestead Gristmill",
                    "packageClientReferenceId": reference,
                    "items": package_items,
                }
            ],
            "channelDetails": {
                "channelType": "AMAZON",
                "amazonOrderDetails": {"orderId": amazon_order_id},
            },
        }

        data = self._request(
            "POST",
            "/shipping/v2/shipments/rates",
            json_payload=payload,
            retry_read=True,
        )
        result = data.get("payload") or {}
        request_token = result.get("requestToken")
        raw_rates = result.get("rates") or []
        if not request_token or not raw_rates:
            ineligible = result.get("ineligibleRates") or []
            reasons = []
            for rate in ineligible[:5]:
                for reason in rate.get("ineligibilityReasons") or []:
                    message = reason.get("message")
                    if message:
                        reasons.append(str(message)[:240])
            suffix = ": " + "; ".join(reasons) if reasons else ""
            raise AmazonShippingError(
                "Amazon returned no eligible Buy Shipping rates" + suffix
            )

        rates = [self._legacy_rate(rate, request_token) for rate in raw_rates]
        for rate in rates:
            rate["_amazon"]["package_reference"] = reference
        return rates, {
            "is_residential": None,
            "validation_results": None,
            "provider": "amazon_shipping",
            "policy_version": self.POLICY_VERSION,
            "ineligible_rates": result.get("ineligibleRates") or [],
        }

    def purchase_label(self, rate_obj: dict) -> dict:
        meta = rate_obj.get("_amazon") or {}
        document_spec = meta.get("document_spec")
        if not document_spec:
            return {"error": "Selected Amazon rate does not support a ZPL label."}
        if meta.get("requires_additional_inputs"):
            return {
                "error": "Selected Amazon rate requires additional inputs and was not purchased."
            }
        if meta.get("unresolved_required_vas_groups"):
            return {
                "error": "Selected Amazon rate has unresolved required value-added services."
            }

        payload = {
            "requestToken": meta.get("request_token"),
            "rateId": meta.get("rate_id") or rate_obj.get("object_id"),
            "requestedDocumentSpecification": document_spec,
        }
        if meta.get("requested_value_added_services"):
            payload["requestedValueAddedServices"] = meta[
                "requested_value_added_services"
            ]
        data = self._request(
            "POST",
            "/shipping/v2/shipments",
            json_payload=payload,
            purchase=True,
        )
        result = data.get("payload") or {}
        if not isinstance(result, dict) or not result.get("shipmentId"):
            raise AmazonShippingPurchaseUncertain(
                "Amazon returned a purchase response without a shipment ID.",
                response_data=self._safe_response_data(result),
            )
        package_details = result.get("packageDocumentDetails") or []
        if not package_details:
            raise AmazonShippingPurchaseUncertain(
                "Amazon created a shipment but returned no label document.",
                shipment_id=result.get("shipmentId"),
                response_data=self._safe_response_data(result),
            )

        package = package_details[0]
        documents = package.get("packageDocuments") or []
        label = next(
            (
                document
                for document in documents
                if document.get("type") == "LABEL"
                and str(document.get("format") or "").upper() == "ZPL"
            ),
            None,
        )
        if not label or not label.get("contents"):
            raise AmazonShippingPurchaseUncertain(
                "Amazon created a shipment but returned no ZPL label.",
                shipment_id=result.get("shipmentId"),
                response_data=self._safe_response_data(result),
            )

        try:
            zpl_data = base64.b64decode(label["contents"], validate=True).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise AmazonShippingPurchaseUncertain(
                "Amazon created a shipment but returned an invalid ZPL label.",
                shipment_id=result.get("shipmentId"),
                response_data=self._safe_response_data(result),
            ) from exc

        promise = result.get("promise") or meta.get("promise") or {}
        delivery = promise.get("deliveryWindow") or {}
        charge = result.get("totalChargeWithAdjustments") or result.get("totalCharge") or {}
        return {
            "purchase_provider": "amazon_shipping",
            "provider_shipment_id": result.get("shipmentId"),
            "provider_rate_id": meta.get("rate_id") or rate_obj.get("object_id"),
            "provider_service_id": meta.get("service_id"),
            "tracking_number": package.get("trackingId"),
            "tracking_url": None,
            "label_url": None,
            "label_zpl": zpl_data,
            "label_format": "ZPL",
            "carrier": rate_obj.get("provider"),
            "service": (rate_obj.get("servicelevel") or {}).get("name"),
            "rate_amount": float(
                charge.get("value")
                if charge.get("value") is not None
                else rate_obj.get("amount") or 0
            ),
            "rate_currency": charge.get("unit") or rate_obj.get("currency") or "USD",
            "promised_delivery_at": delivery.get("endTime") or delivery.get("end"),
            "provider_response_json": json.dumps(
                self._safe_response_data(result), sort_keys=True
            ),
        }

    @classmethod
    def _safe_response_data(cls, value):
        """Remove document bytes and token-like values before persistence."""
        if isinstance(value, dict):
            safe = {}
            for key, child in value.items():
                if str(key).lower() in {
                    "contents",
                    "access_token",
                    "refresh_token",
                    "client_secret",
                }:
                    safe[key] = "[redacted]"
                else:
                    safe[key] = cls._safe_response_data(child)
            return safe
        if isinstance(value, list):
            return [cls._safe_response_data(item) for item in value]
        return value

    def cancel_shipment(self, shipment_id: str) -> dict:
        shipment_id = (shipment_id or "").strip()
        if not shipment_id:
            return {"error": "Missing Amazon shipment ID"}
        try:
            self._request(
                "PUT",
                f"/shipping/v2/shipments/{quote(shipment_id, safe='')}/cancel",
                purchase=True,
            )
        except AmazonShippingPurchaseUncertain as exc:
            return {"error": str(exc), "status": "uncertain"}
        except AmazonShippingError as exc:
            return {"error": str(exc), "status": "error"}
        return {"status": "success"}

    def get_shipment_documents(
        self, shipment_id: str, package_reference: str, *, dpi: int = 203
    ) -> dict:
        """Retrieve a purchased ZPL label for manual uncertainty reconciliation."""
        shipment_id = (shipment_id or "").strip()
        package_reference = (package_reference or "").strip()
        if not shipment_id or not package_reference:
            return {"error": "Amazon shipment ID and package reference are required"}
        data = self._request(
            "GET",
            f"/shipping/v2/shipments/{quote(shipment_id, safe='')}/documents",
            params={
                "packageClientReferenceId": package_reference,
                "format": "ZPL",
                "dpi": dpi,
            },
            retry_read=True,
        )
        result = data.get("payload") or {}
        package = result.get("packageDocumentDetail") or {}
        documents = package.get("packageDocuments") or []
        label = next(
            (
                document
                for document in documents
                if document.get("type") == "LABEL"
                and str(document.get("format") or "").upper() == "ZPL"
            ),
            None,
        )
        if not label or not label.get("contents"):
            return {"error": "Amazon returned no ZPL label document"}
        try:
            zpl_data = base64.b64decode(label["contents"], validate=True).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError):
            return {"error": "Amazon returned an invalid ZPL label document"}
        return {
            "provider_shipment_id": shipment_id,
            "tracking_number": package.get("trackingId"),
            "label_zpl": zpl_data,
            "label_format": "ZPL",
            "provider_response_json": json.dumps(
                self._safe_response_data(result), sort_keys=True
            ),
        }
