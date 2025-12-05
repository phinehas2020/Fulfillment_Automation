import logging
import requests
import json
from odoo import exceptions

_logger = logging.getLogger(__name__)

class ShippoService:
    API_URL = "https://api.goshippo.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    @classmethod
    def from_env(cls, env):
        ICP = env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("shippo.api_key")
        # Return None if not configured, allowing caller to handle fallback
        if not api_key:
            return None
        return cls(api_key)

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
            "phone": order.shipping_phone or "",
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
            "phone": sender_company.phone or "",
            "email": sender_company.email or "no-reply@example.com",
        }

        # 2. Prepare Parcel
        # Odoo stores weight in grams (from our shopify_order model)
        # Box dimensions in our system are typically inches. 
        # API expects: weight in 'g', 'oz', 'lb', 'kg'. distance_unit in 'in', 'cm', 'mm', 'm', 'yd'.
        parcel = {
            "length": box.length,
            "width": box.width,
            "height": box.height,
            "distance_unit": "in",
            "weight": order.total_weight,
            "mass_unit": "g",
        }

        payload = {
            "address_from": address_from,
            "address_to": address_to,
            "parcels": [parcel],
            "async": False,
        }

        _logger.info("Shippo: Creating shipment for Order %s", order.id)
        
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=15)
            if resp.status_code >= 400:
                _logger.error("Shippo Error: %s", resp.text)
                return []
            
            data = resp.json()
            return data.get("rates", [])
        except Exception as e:
            _logger.exception("Failed to connect to Shippo")
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
            if resp.status_code >= 400:
                _logger.error("Shippo Transaction Error: %s", resp.text)
                return None
            
            data = resp.json()
            status = data.get("status")
            
            if status != "SUCCESS":
                _logger.error("Shippo Transaction status: %s Messages: %s", status, data.get("messages"))
                return None
            
            label_url = data.get("label_url")
            zpl_data = None
            
            # Download ZPL content immediately if we have a URL
            if label_url and data.get("label_file_type") == "ZPLII":
                 zpl_data = self._download_url(label_url)
            
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
            _logger.exception("Shippo Purchase Failed")
            return None

    def _download_url(self, url):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return r.text
        except Exception:
            _logger.exception("Failed to download label content from %s", url)
        return None
