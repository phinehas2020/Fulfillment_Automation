from odoo import http
from odoo.http import request


class PrintAgentController(http.Controller):
    """Endpoints for Raspberry Pi print agent polling + job completion."""

    @http.route("/print-agent/poll", type="json", auth="public", methods=["GET"])
    def poll(self, printer_id=None, **kwargs):
        # TODO: secure with API key, return pending jobs filtered by printer_id
        _ = kwargs  # placeholder to silence lint
        jobs = []
        return {"printer_id": printer_id, "jobs": jobs}

    @http.route("/print-agent/complete", type="json", auth="public", methods=["POST"])
    def complete(self, **kwargs):
        # TODO: mark job completed/failed and advance fulfillment workflow
        payload = request.jsonrequest
        return {"received": payload}


