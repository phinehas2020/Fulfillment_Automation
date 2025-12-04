"""HTTP client for communicating with Odoo print job endpoints (skeleton)."""

from typing import List

import requests

from config import ODOO_API_KEY, PRINTER_ID


class OdooClient:
    def __init__(self, base_url: str, api_key: str = ODOO_API_KEY, printer_id: str = PRINTER_ID):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.printer_id = printer_id

    def fetch_pending_jobs(self) -> List[dict]:
        # TODO: call /print-agent/poll with auth header
        return []

    def mark_complete(self, job_id: int, success: bool, error: str | None = None):
        # TODO: call /print-agent/complete with auth header
        _ = (job_id, success, error)
        return {"status": "stub"}


