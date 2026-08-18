"""Pure unit coverage for the Amazon Shipping API v2 adapter.

The addon normally runs inside Odoo.  These tests load the service directly
with a tiny Odoo stub so API request/response behavior can be tested without a
database or an Odoo installation.
"""

import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SERVICES_ROOT = ROOT / "shopify_fulfillment" / "services"


def _load_service_module():
    """Load amazon_shipping_service and its lightweight dependencies."""
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
        if module_name in sys.modules:
            continue
        dependency_spec = importlib.util.spec_from_file_location(
            module_name, SERVICES_ROOT / filename
        )
        dependency = importlib.util.module_from_spec(dependency_spec)
        sys.modules[module_name] = dependency
        dependency_spec.loader.exec_module(dependency)

    module_name = "shopify_fulfillment.services.amazon_shipping_service"
    service_spec = importlib.util.spec_from_file_location(
        module_name, SERVICES_ROOT / "amazon_shipping_service.py"
    )
    service = importlib.util.module_from_spec(service_spec)
    sys.modules[module_name] = service
    service_spec.loader.exec_module(service)
    return service


amazon_service = _load_service_module()
AmazonShippingError = amazon_service.AmazonShippingError
AmazonShippingPurchaseUncertain = amazon_service.AmazonShippingPurchaseUncertain
AmazonShippingService = amazon_service.AmazonShippingService


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"{}"):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _LineCollection(list):
    def filtered(self, predicate):
        return _LineCollection(line for line in self if predicate(line))


class _Line:
    def __init__(self, line_id=7, sku="SKU-1", title="Fresh wheat", weight=500):
        self.id = line_id
        self.sku = sku
        self.title = title
        self.weight = weight
        self.unit_price = 4.25
        self.amazon_asin = ""
        self.amazon_order_item_id = None
        self.writes = []

    def sudo(self):
        return self

    def write(self, values):
        self.writes.append(values)
        self.amazon_order_item_id = values.get("amazon_order_item_id")


def _service(**overrides):
    AmazonShippingService._token_cache.clear()
    values = {
        "client_id": "client-1",
        "client_secret": "secret-1",
        "refresh_token": "refresh-1",
    }
    values.update(overrides)
    return AmazonShippingService(**values)


class LwaTokenTest(unittest.TestCase):
    def setUp(self):
        AmazonShippingService._token_cache.clear()

    @patch.object(amazon_service.requests, "post")
    def test_token_is_cached_and_lwa_request_uses_expected_form(self, post):
        response = _FakeResponse(
            payload={"access_token": "access-1", "expires_in": 3600}
        )
        post.return_value = response
        service = _service()

        self.assertEqual(service._access_token(), "access-1")
        self.assertEqual(service._access_token(), "access-1")

        post.assert_called_once_with(
            service.LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
                "client_id": "client-1",
                "client_secret": "secret-1",
            },
            timeout=15,
        )

    @patch.object(amazon_service.requests, "post")
    def test_auth_error_does_not_redact_credentials_into_exception(self, post):
        post.return_value = _FakeResponse(status_code=401, payload={})
        service = _service()

        with self.assertRaises(AmazonShippingError) as raised:
            service._access_token()

        message = str(raised.exception)
        self.assertNotIn(service.client_id, message)
        self.assertNotIn(service.client_secret, message)
        self.assertNotIn(service.refresh_token, message)

    @patch.object(amazon_service.requests, "post")
    def test_cache_isolated_by_refresh_token(self, post):
        post.side_effect = [
            _FakeResponse(payload={"access_token": "access-1", "expires_in": 3600}),
            _FakeResponse(payload={"access_token": "access-2", "expires_in": 3600}),
        ]

        first = _service(refresh_token="refresh-1")
        second = AmazonShippingService(
            client_id="client-1",
            client_secret="secret-1",
            refresh_token="refresh-2",
        )

        self.assertEqual(first._access_token(), "access-1")
        self.assertEqual(second._access_token(), "access-2")
        self.assertEqual(post.call_count, 2)


class OrderItemsTest(unittest.TestCase):
    def test_order_items_quotes_order_id_and_are_cached(self):
        service = _service()
        order_id = "AMZ/order?1"
        payload = {"payload": {"OrderItems": [{"OrderItemId": "item-1"}]}}

        with patch.object(service, "_request", return_value=payload) as request:
            first = service.get_order_items(order_id)
            second = service.get_order_items(order_id)

        self.assertEqual(first, second)
        request.assert_called_once_with(
            "GET",
            "/orders/v0/orders/AMZ%2Forder%3F1/orderItems",
            params=None,
            retry_read=True,
        )

    def test_missing_or_empty_order_items_are_rejected(self):
        service = _service()
        with self.assertRaises(AmazonShippingError):
            with patch.object(service, "_request", return_value={"payload": {}}):
                service.get_order_items("AMZ-1")
        with self.assertRaises(amazon_service.AmazonShippingConfigurationError):
            service.get_order_items("  ")


class RateRequestTest(unittest.TestCase):
    def test_rate_request_normalizes_rate_and_preserves_amazon_metadata(self):
        service = _service()
        line = _Line()
        order = SimpleNamespace(
            amazon_order_id="AMZ-1",
            order_currency="USD",
            order_name="#1001",
            id=1001,
            line_ids=_LineCollection([line]),
        )
        packed_box = SimpleNamespace(line_ids=[line.id], line_quantities={line.id: 2})
        box = SimpleNamespace(length=12, width=8, height=5)
        company = SimpleNamespace(
            name="Homestead Gristmill",
            street="123 Main St\nSuite 4",
            street2="Suite 4",
            city="Waco",
            state_id=SimpleNamespace(code="TX"),
            zip="76701",
            country_id=SimpleNamespace(code="US"),
            email="shipping@example.test",
            phone="+1 254-555-0100",
        )
        amazon_items = [
            {
                "OrderItemId": "item-1",
                "SellerSKU": "SKU-1",
                "ASIN": "B0001",
                "QuantityOrdered": 2,
                "ItemPrice": {"Amount": "8.50", "CurrencyCode": "USD"},
            }
        ]
        response = {
            "payload": {
                "requestToken": "request-1",
                "rates": [
                    {
                        "rateId": "rate-1",
                        "carrierId": "AMZN",
                        "carrierName": "Amazon Shipping",
                        "serviceId": "amazon_standard",
                        "serviceName": "Standard",
                        "totalChargeWithAdjustments": {"value": "7.50", "unit": "USD"},
                        "promise": {"deliveryWindow": {"endTime": "2026-08-21T20:00:00Z"}},
                        "supportedDocumentSpecifications": [
                            {
                                "format": "ZPL",
                                "size": {"width": 4, "length": 6, "unit": "INCH"},
                                "printOptions": [
                                    {
                                        "supportedDPIs": [300, 203],
                                        "supportedPageLayouts": ["DEFAULT"],
                                        "supportedFileJoiningOptions": [True, False],
                                        "supportedDocumentDetails": [{"name": "LABEL"}],
                                    }
                                ],
                            }
                        ],
                        "benefits": ["TRACKING"],
                    }
                ],
                "ineligibleRates": [],
            }
        }

        with patch.object(service, "get_order_items", return_value=amazon_items), patch.object(
            service, "_request", return_value=response
        ) as request:
            rates, metadata = service.get_rates_for_box(
                order=order,
                packed_box=packed_box,
                box=box,
                total_weight_grams=1500,
                sender_company=company,
                sequence=2,
            )

        request.assert_called_once()
        method, path = request.call_args.args
        self.assertEqual((method, path), ("POST", "/shipping/v2/shipments/rates"))
        self.assertTrue(request.call_args.kwargs["retry_read"])
        request_payload = request.call_args.kwargs["json_payload"]
        package = request_payload["packages"][0]
        self.assertEqual(package["packageClientReferenceId"], "#1001-box-2")
        self.assertEqual(package["items"][0]["itemIdentifier"], "item-1")
        self.assertEqual(package["items"][0]["quantity"], 2)
        self.assertEqual(package["items"][0]["itemValue"], {"unit": "USD", "value": 4.25})
        self.assertEqual(package["dimensions"]["unit"], "INCH")
        self.assertEqual(request_payload["channelDetails"], {
            "channelType": "AMAZON",
            "amazonOrderDetails": {"orderId": "AMZ-1"},
        })
        self.assertEqual(line.writes, [{"amazon_order_item_id": "item-1"}])

        rate = rates[0]
        self.assertEqual(rate["amount"], "7.50")
        self.assertEqual(rate["currency"], "USD")
        self.assertEqual(rate["arrives_by"], "2026-08-21T20:00:00Z")
        self.assertEqual(rate["_amazon"]["request_token"], "request-1")
        self.assertEqual(rate["_amazon"]["rate_id"], "rate-1")
        self.assertEqual(rate["_amazon"]["benefits"], ["TRACKING"])
        self.assertEqual(metadata["provider"], "amazon_shipping")
        self.assertEqual(metadata["policy_version"], AmazonShippingService.POLICY_VERSION)


class DocumentSpecTest(unittest.TestCase):
    def test_zpl_spec_prefers_203_dpi_and_never_joins_when_false_supported(self):
        rate = {
            "supportedDocumentSpecifications": [
                {
                    "format": "PDF",
                    "printOptions": [{"supportedDocumentDetails": [{"name": "LABEL"}]}],
                },
                {
                    "format": "zpl",
                    "size": {"width": 4, "length": 6, "unit": "INCH"},
                    "printOptions": [
                        {
                            "supportedDPIs": [300, 203],
                            "supportedPageLayouts": ["ROTATED"],
                            "supportedFileJoiningOptions": [True, False],
                            "supportedDocumentDetails": [{"name": "LABEL"}],
                        }
                    ],
                },
            ]
        }

        self.assertEqual(
            AmazonShippingService._zpl_document_spec(rate),
            {
                "format": "ZPL",
                "size": {"width": 4, "length": 6, "unit": "INCH"},
                "pageLayout": "ROTATED",
                "needFileJoining": False,
                "requestedDocumentTypes": ["LABEL"],
                "dpi": 203,
            },
        )

    def test_zpl_spec_requires_label_document_detail(self):
        rate = {
            "supportedDocumentSpecifications": [{
                "format": "ZPL",
                "printOptions": [{"supportedDocumentDetails": [{"name": "INVOICE"}]}],
            }]
        }
        self.assertIsNone(AmazonShippingService._zpl_document_spec(rate))

    def test_zpl_spec_rejects_non_four_by_six_labels(self):
        rate = {
            "supportedDocumentSpecifications": [{
                "format": "ZPL",
                "size": {"width": 4, "length": 8, "unit": "INCH"},
                "printOptions": [{
                    "supportedDPIs": [203],
                    "supportedPageLayouts": ["DEFAULT"],
                    "supportedFileJoiningOptions": [False],
                    "supportedDocumentDetails": [{"name": "LABEL"}],
                }],
            }]
        }
        self.assertIsNone(AmazonShippingService._zpl_document_spec(rate))

    def test_required_vas_is_selected_only_when_unambiguous_or_configured(self):
        service = _service(
            required_vas_preferences={"PICKUP": "SHIPPER_DROPOFF"}
        )
        selected, unresolved = service._required_value_added_services({
            "availableValueAddedServiceGroups": [
                {
                    "groupId": "PICKUP",
                    "isRequired": True,
                    "valueAddedServices": [
                        {"id": "CARRIER_PICKUP"},
                        {"id": "SHIPPER_DROPOFF"},
                    ],
                },
                {
                    "groupId": "CONFIRMATION",
                    "isRequired": True,
                    "valueAddedServices": [{"id": "DELIVERY_CONFIRMATION"}],
                },
            ]
        })

        self.assertEqual(
            selected,
            [{"id": "SHIPPER_DROPOFF"}, {"id": "DELIVERY_CONFIRMATION"}],
        )
        self.assertEqual(unresolved, [])

    def test_required_vas_with_multiple_unconfigured_options_is_unresolved(self):
        service = _service()
        selected, unresolved = service._required_value_added_services({
            "availableValueAddedServiceGroups": [{
                "groupId": "PICKUP",
                "isRequired": True,
                "valueAddedServices": [
                    {"id": "CARRIER_PICKUP"},
                    {"id": "SHIPPER_DROPOFF"},
                ],
            }]
        })

        self.assertEqual(selected, [])
        self.assertEqual(unresolved, ["PICKUP"])


class PurchaseAndCancellationTest(unittest.TestCase):
    def test_purchase_decodes_zpl_and_returns_charge_and_promise(self):
        service = _service()
        zpl = "^XA^FO20,20^FDTEST^FS^XZ"
        rate = {
            "object_id": "rate-1",
            "provider": "Amazon Shipping",
            "servicelevel": {"name": "Standard"},
            "amount": "7.50",
            "currency": "USD",
            "_amazon": {
                "request_token": "request-1",
                "rate_id": "rate-1",
                "service_id": "amazon_standard",
                "document_spec": {"format": "ZPL", "requestedDocumentTypes": ["LABEL"]},
                "promise": {},
            },
        }
        response = {
            "payload": {
                "shipmentId": "shipment-1",
                "packageDocumentDetails": [{
                    "trackingId": "tracking-1",
                    "packageDocuments": [{
                        "type": "LABEL",
                        "format": "ZPL",
                        "contents": base64.b64encode(zpl.encode()).decode(),
                    }],
                }],
                "totalChargeWithAdjustments": {"value": "8.25", "unit": "USD"},
                "promise": {"deliveryWindow": {"endTime": "2026-08-21T20:00:00Z"}},
            }
        }

        with patch.object(service, "_request", return_value=response) as request:
            result = service.purchase_label(rate)

        request.assert_called_once_with(
            "POST",
            "/shipping/v2/shipments",
            json_payload={
                "requestToken": "request-1",
                "rateId": "rate-1",
                "requestedDocumentSpecification": rate["_amazon"]["document_spec"],
            },
            purchase=True,
        )
        self.assertEqual(result["provider_shipment_id"], "shipment-1")
        self.assertEqual(result["tracking_number"], "tracking-1")
        self.assertEqual(result["label_zpl"], zpl)
        self.assertEqual(result["rate_amount"], 8.25)
        self.assertEqual(result["promised_delivery_at"], "2026-08-21T20:00:00Z")

    def test_purchase_includes_resolved_required_value_added_services(self):
        service = _service()
        zpl = base64.b64encode(b"^XA^XZ").decode()
        rate = {
            "object_id": "rate-vas",
            "provider": "Amazon Shipping",
            "servicelevel": {"name": "Standard"},
            "amount": "7.50",
            "currency": "USD",
            "_amazon": {
                "request_token": "request-vas",
                "rate_id": "rate-vas",
                "service_id": "amazon_standard",
                "document_spec": {
                    "format": "ZPL",
                    "requestedDocumentTypes": ["LABEL"],
                },
                "requested_value_added_services": [
                    {"id": "SHIPPER_DROPOFF"}
                ],
            },
        }
        response = {
            "payload": {
                "shipmentId": "shipment-vas",
                "packageDocumentDetails": [{
                    "trackingId": "tracking-vas",
                    "packageDocuments": [{
                        "type": "LABEL",
                        "format": "ZPL",
                        "contents": zpl,
                    }],
                }],
            }
        }

        with patch.object(service, "_request", return_value=response) as request:
            service.purchase_label(rate)

        payload = request.call_args.kwargs["json_payload"]
        self.assertEqual(
            payload["requestedValueAddedServices"],
            [{"id": "SHIPPER_DROPOFF"}],
        )

    def test_partial_purchase_response_is_uncertain_and_preserves_shipment_id(self):
        service = _service()
        rate = {
            "object_id": "rate-partial",
            "provider": "Amazon Shipping",
            "servicelevel": {"name": "Standard"},
            "amount": "7.50",
            "currency": "USD",
            "_amazon": {
                "request_token": "request-partial",
                "rate_id": "rate-partial",
                "document_spec": {
                    "format": "ZPL",
                    "requestedDocumentTypes": ["LABEL"],
                },
            },
        }
        response = {
            "payload": {
                "shipmentId": "shipment-partial",
                "packageDocumentDetails": [],
                "access_token": "must-not-persist",
            }
        }

        with patch.object(service, "_request", return_value=response):
            with self.assertRaises(AmazonShippingPurchaseUncertain) as raised:
                service.purchase_label(rate)

        self.assertEqual(raised.exception.shipment_id, "shipment-partial")
        self.assertEqual(
            raised.exception.response_data["access_token"], "[redacted]"
        )

    def test_purchase_response_without_shipment_id_is_uncertain(self):
        service = _service()
        rate = {
            "object_id": "rate-no-shipment",
            "provider": "Amazon Shipping",
            "servicelevel": {"name": "Standard"},
            "amount": "7.50",
            "currency": "USD",
            "_amazon": {
                "request_token": "request-no-shipment",
                "rate_id": "rate-no-shipment",
                "document_spec": {
                    "format": "ZPL",
                    "requestedDocumentTypes": ["LABEL"],
                },
            },
        }

        with patch.object(service, "_request", return_value={"payload": {}}):
            with self.assertRaises(AmazonShippingPurchaseUncertain):
                service.purchase_label(rate)

    @patch.object(amazon_service.requests, "request")
    def test_purchase_timeout_is_uncertain_and_is_not_retried(self, request):
        request.side_effect = amazon_service.requests.Timeout("timed out")
        service = _service()

        with patch.object(service, "_headers", return_value={}), patch.object(
            amazon_service.time, "sleep"
        ) as sleep:
            with self.assertRaises(AmazonShippingPurchaseUncertain):
                service._request("POST", "/shipping/v2/shipments", purchase=True)

        request.assert_called_once()
        sleep.assert_not_called()

    @patch.object(amazon_service.requests, "request")
    def test_transient_mutation_response_is_uncertain_and_is_not_retried(self, request):
        request.return_value = _FakeResponse(
            status_code=503,
            payload={"errors": [{"code": "ServiceUnavailable", "message": "retry"}]},
            content=b"error",
        )
        service = _service()

        with patch.object(service, "_headers", return_value={}), patch.object(
            amazon_service.time, "sleep"
        ) as sleep:
            with self.assertRaises(AmazonShippingPurchaseUncertain):
                service._request("POST", "/shipping/v2/shipments", purchase=True)

        request.assert_called_once()
        sleep.assert_not_called()

    def test_cancellation_uses_encoded_shipment_path_and_maps_uncertain_state(self):
        service = _service()
        with patch.object(service, "_request", return_value={}) as request:
            self.assertEqual(service.cancel_shipment("ship/id 1"), {"status": "success"})
        request.assert_called_once_with(
            "PUT", "/shipping/v2/shipments/ship%2Fid%201/cancel", purchase=True
        )

        with patch.object(
            service,
            "_request",
            side_effect=AmazonShippingPurchaseUncertain("reconcile first"),
        ):
            result = service.cancel_shipment("ship-1")
        self.assertEqual(result, {"error": "reconcile first", "status": "uncertain"})

        with patch.object(
            service, "_request", side_effect=AmazonShippingError("rejected")
        ):
            result = service.cancel_shipment("ship-1")
        self.assertEqual(result, {"error": "rejected", "status": "error"})

        with patch.object(service, "_request") as request:
            self.assertEqual(service.cancel_shipment("  "), {"error": "Missing Amazon shipment ID"})
        request.assert_not_called()

    def test_document_reconciliation_recovers_existing_zpl_without_purchase(self):
        service = _service()
        zpl = "^XA^FDRECOVERED^FS^XZ"
        response = {
            "payload": {
                "packageDocumentDetail": {
                    "trackingId": "tracking-recovered",
                    "packageDocuments": [{
                        "type": "LABEL",
                        "format": "ZPL",
                        "contents": base64.b64encode(zpl.encode()).decode(),
                    }],
                }
            }
        }

        with patch.object(service, "_request", return_value=response) as request:
            result = service.get_shipment_documents(
                "shipment/id", "#1001-box-1"
            )

        request.assert_called_once_with(
            "GET",
            "/shipping/v2/shipments/shipment%2Fid/documents",
            params={
                "packageClientReferenceId": "#1001-box-1",
                "format": "ZPL",
                "dpi": 203,
            },
            retry_read=True,
        )
        self.assertEqual(result["tracking_number"], "tracking-recovered")
        self.assertEqual(result["label_zpl"], zpl)


if __name__ == "__main__":
    unittest.main()
