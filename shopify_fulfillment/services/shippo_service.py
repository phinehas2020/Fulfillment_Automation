import logging
import re
import requests
import json
from odoo import exceptions

_logger = logging.getLogger(__name__)


def sanitize_phone(phone: str) -> str:
    """Clean phone number for shipping APIs.
    
    Removes extensions (e.g., "ext. 12345", "x123", "extension 456")
    and ensures only valid phone characters remain.
    """
    if not phone:
        return ""
    
    # Remove extension patterns: "ext. 123", "ext 123", "x123", "extension 123", etc.
    phone = re.sub(r'\s*(ext\.?|extension|x)\s*\d+.*$', '', phone, flags=re.IGNORECASE)
    
    # Keep only digits, spaces, dashes, parentheses, and plus sign
    phone = re.sub(r'[^\d\s\-\(\)\+]', '', phone)
    
    # Clean up extra whitespace
    phone = ' '.join(phone.split())
    
    return phone.strip()

class ShippoService:
    API_URL = "https://api.goshippo.com"

    def __init__(self, api_key: str, shipper_phone: str = None):
        self.api_key = api_key
        self.shipper_phone = shipper_phone or "555-555-5555"

    @classmethod
    def from_env(cls, env):
        ICP = env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("shippo.api_key")
        shipper_phone = ICP.get_param("shippo.shipper_phone")
        # Return None if not configured, allowing caller to handle fallback
        if not api_key:
            return None
        return cls(api_key, shipper_phone)

    def _headers(self):
        return {
            "Authorization": f"ShippoToken {self.api_key}",
            "Content-Type": "application/json",
        }

    def get_rates(self, order, box, sender_company):
        url = f"{self.API_URL}/shipments"

        # 1. Prepare Addresses
        # Ensure we have defaults for missing fields to avoid 400 errors
        address_to = {
            "name": order.customer_name or "Customer",
            "street1": order.shipping_address_line1 or "",
            "street2": order.shipping_address_line2 or "",
            "city": order.shipping_city or "",
            "state": order.shipping_state or "",
            "zip": order.shipping_zip or "",
            "country": order.shipping_country or "US",
            "phone": sanitize_phone(order.shipping_phone),
            "email": order.email or "no-reply@example.com",
        }

        address_from = {
            "name": sender_company.name,
            "street1": sender_company.street or "",
            "street2": sender_company.street2 or "",
            "city": sender_company.city or "",
            "state": sender_company.state_id.code if sender_company.state_id else "",
            "zip": sender_company.zip or "",
            "country": sender_company.country_id.code if sender_company.country_id else "US",
            "phone": sender_company.phone or self.shipper_phone,
            "email": sender_company.email or "no-reply@example.com",
        }

        # 2. Prepare Parcel
        # Odoo stores weight in grams (from our shopify_order model)
        # Box dimensions in our system are typically inches. 
        # API expects: weight in 'g', 'oz', 'lb', 'kg'. distance_unit in 'in', 'cm', 'mm', 'm', 'yd'.
        # Box weight is in Ounces, Order weight is in Grams.
        # We need to add them together.
        box_weight_oz = box.box_weight or 0.0
        box_weight_g = box_weight_oz * 28.3495
        total_weight_g = (order.total_weight or 0.0) + box_weight_g

        parcel = {
            "length": box.length,
            "width": box.width,
            "height": box.height,
            "distance_unit": "in",
            "weight": total_weight_g,
            "mass_unit": "g",
        }

        payload = {
            "address_from": address_from,
            "address_to": address_to,
            "parcels": [parcel],
            "async": False,
        }

        _logger.info("Shippo: Creating shipment for Order %s", order.id)
        _logger.info("Shippo: From: %s, %s %s", address_from.get("city"), address_from.get("state"), address_from.get("zip"))
        _logger.info("Shippo: To: %s, %s %s", address_to.get("city"), address_to.get("state"), address_to.get("zip"))
        _logger.info("Shippo: Parcel: %sx%sx%s in, %s g", parcel["length"], parcel["width"], parcel["height"], parcel["weight"])
        
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=15)
            _logger.info("Shippo: Response status: %s", resp.status_code)
            
            if resp.status_code >= 400:
                _logger.error("Shippo Error: %s", resp.text)
                return []
            
            data = resp.json()
            rates = data.get("rates", [])
            messages = data.get("messages", [])
            
            _logger.info("Shippo: Got %d rates", len(rates))
            if messages:
                _logger.warning("Shippo messages: %s", messages)
            if rates:
                for r in rates[:3]:  # Log first 3 rates
                    _logger.info("  Rate: %s %s - $%s", 
                                r.get("provider"), r.get("servicelevel", {}).get("name"), r.get("amount"))
            
            return rates
        except Exception as e:
            _logger.exception("Failed to connect to Shippo: %s", e)
            return []

    def get_rates_for_box(self, order, box, total_weight_grams: float, sender_company):
        """Get shipping rates for a specific box with explicit weight.

        This method is used for multi-box shipments where each box has its own
        weight calculated by the packing algorithm.

        Args:
            order: shopify.order record (for addresses)
            box: fulfillment.box record (for dimensions)
            total_weight_grams: Pre-calculated total weight including box (in grams)
            sender_company: res.company record (sender address)

        Returns:
            List of rate objects from Shippo
        """
        url = f"{self.API_URL}/shipments"

        # Prepare addresses (same as get_rates)
        address_to = {
            "name": order.customer_name or "Customer",
            "street1": order.shipping_address_line1 or "",
            "street2": order.shipping_address_line2 or "",
            "city": order.shipping_city or "",
            "state": order.shipping_state or "",
            "zip": order.shipping_zip or "",
            "country": order.shipping_country or "US",
            "phone": sanitize_phone(order.shipping_phone),
            "email": order.email or "no-reply@example.com",
        }

        address_from = {
            "name": sender_company.name,
            "street1": sender_company.street or "",
            "street2": sender_company.street2 or "",
            "city": sender_company.city or "",
            "state": sender_company.state_id.code if sender_company.state_id else "",
            "zip": sender_company.zip or "",
            "country": sender_company.country_id.code if sender_company.country_id else "US",
            "phone": sender_company.phone or self.shipper_phone,
            "email": sender_company.email or "no-reply@example.com",
        }

        # Prepare parcel with explicit weight (already includes box weight)
        parcel = {
            "length": box.length,
            "width": box.width,
            "height": box.height,
            "distance_unit": "in",
            "weight": total_weight_grams,
            "mass_unit": "g",
        }

        payload = {
            "address_from": address_from,
            "address_to": address_to,
            "parcels": [parcel],
            "async": False,
        }

        _logger.info(
            "Shippo (multi-box): Creating shipment for Order %s, Box %s",
            order.id,
            box.name,
        )
        _logger.info(
            "Shippo: Parcel: %sx%sx%s in, %.0f g",
            parcel["length"],
            parcel["width"],
            parcel["height"],
            parcel["weight"],
        )

        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=15)
            _logger.info("Shippo: Response status: %s", resp.status_code)

            if resp.status_code >= 400:
                _logger.error("Shippo Error: %s", resp.text)
                return []

            data = resp.json()
            rates = data.get("rates", [])
            messages = data.get("messages", [])

            _logger.info("Shippo: Got %d rates for box %s", len(rates), box.name)
            if messages:
                _logger.warning("Shippo messages: %s", messages)
            if rates:
                for r in rates[:3]:
                    _logger.info(
                        "  Rate: %s %s - $%s",
                        r.get("provider"),
                        r.get("servicelevel", {}).get("name"),
                        r.get("amount"),
                    )

            return rates
        except Exception as e:
            _logger.exception("Failed to connect to Shippo: %s", e)
            return []

    def purchase_label(self, rate_obj):
        """
        Purchase the label for the given rate object (from get_rates).
        """
        url = f"{self.API_URL}/transactions"
        rate_id = rate_obj.get("object_id")
        
        payload = {
            "rate": rate_id,
            "label_file_type": "ZPLII",
            "async": False
        }
        
        _logger.info("Shippo: Buying label for rate %s", rate_id)

        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=20)
            _logger.info("Shippo Transaction Response Status: %s", resp.status_code)
            
            if resp.status_code >= 400:
                _logger.error("Shippo Transaction Error: %s", resp.text)
                return None
            
            data = resp.json()
            status = data.get("status")
            label_file_type = data.get("label_file_type")
            label_url = data.get("label_url")
            
            _logger.info("Shippo Transaction Result - Status: %s, FileType: %s, URL: %s", 
                        status, label_file_type, label_url)
            
            if status != "SUCCESS":
                messages = data.get("messages", [])
                error_msg = "Unknown error"
                error_codes = []
                carrier_source = None
                if messages and isinstance(messages, list):
                    # Shippo messages often look like [{'text': '...', 'code': '...', 'source': '...'}]
                    error_msg = "; ".join([m.get("text", str(m)) for m in messages])
                    error_codes = [m.get("code", "") for m in messages if m.get("code")]
                    # Get the carrier that failed
                    sources = [m.get("source", "") for m in messages if m.get("source")]
                    if sources:
                        carrier_source = sources[0]
                
                _logger.error("Shippo Transaction status: %s Messages: %s", status, messages)
                return {
                    "error": error_msg, 
                    "error_codes": error_codes,
                    "failed_carrier": carrier_source,
                }
            
            zpl_data = None
            
            # Download ZPL content immediately if we have a URL
            if label_url:
                _logger.info("Downloading label from: %s", label_url)
                zpl_data = self._download_url(label_url)
                if zpl_data:
                    _logger.info("Downloaded ZPL label: %d bytes, starts with: %s...", 
                                len(zpl_data), zpl_data[:100] if len(zpl_data) > 100 else zpl_data)
                else:
                    _logger.warning("Failed to download ZPL from %s", label_url)
            else:
                _logger.warning("No label_url in Shippo response")
            
            return {
                "tracking_number": data.get("tracking_number"),
                "tracking_url": data.get("tracking_url_provider"),
                "label_url": label_url,
                "label_zpl": zpl_data,
                "carrier": rate_obj.get("provider"),
                "service": rate_obj.get("servicelevel", {}).get("name"),
                "rate_amount": float(rate_obj.get("amount")),
                "rate_currency": rate_obj.get("currency"),
            }

        except Exception as e:
            _logger.exception("Shippo Purchase Failed: %s", e)
            return None

    def _download_url(self, url):
        try:
            _logger.info("Attempting to download from URL: %s", url)
            r = requests.get(url, timeout=10)
            _logger.info("Download response: status=%s, content-type=%s, length=%d", 
                        r.status_code, r.headers.get('content-type'), len(r.text))
            if r.status_code == 200:
                return r.text
            else:
                _logger.error("Download failed with status %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            _logger.exception("Failed to download label content from %s: %s", url, e)
        return None
