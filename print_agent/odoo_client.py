"""HTTP client for communicating with Odoo print job endpoints (skeleton)."""

from typing import List

import requests

from config import ODOO_API_KEY, PRINTER_ID


class OdooClient:
    def __init__(self, base_url: str, api_key: str = ODOO_API_KEY, printer_id: str = PRINTER_ID):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.printer_id = printer_id

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def fetch_pending_jobs(self) -> List[dict]:
        url = f"{self.base_url}/print-agent/poll"
        resp = requests.get(url, headers=self._headers(), params={"printer_id": self.printer_id}, timeout=15)
        if resp.status_code >= 400:
            return []
        data = resp.json()
        return data.get("jobs", [])

    def mark_complete(self, job_id: int, success: bool, error: str | None = None):
        url = f"{self.base_url}/print-agent/complete"
        payload = {"job_id": job_id, "success": success, "error_message": error}
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=15)
        if resp.status_code >= 400:
            return {"status": "error", "detail": resp.text}
        return resp.json()


