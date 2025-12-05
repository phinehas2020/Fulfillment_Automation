import json
from odoo import http
from odoo.http import request, Response

from ..services.shopify_api import ShopifyAPI


class PrintAgentController(http.Controller):
    """Endpoints for Raspberry Pi print agent polling + job completion."""

    @http.route("/print-agent/poll", type="http", auth="public", methods=["GET"], csrf=False)
    def poll(self, printer_id=None, **kwargs):
        if not self._is_authorized():
            return Response("Unauthorized", status=401)

        domain = [("state", "=", "pending")]
        if printer_id:
            domain.append(("printer_id", "in", [False, printer_id]))

        jobs = request.env["print.job"].sudo().search(domain, limit=10)
        for job in jobs:
            job.write({"state": "printing", "attempts": (job.attempts or 0) + 1})

        payload = [
            {
                "id": job.id,
                "job_type": job.job_type,
                "zpl_data": job.zpl_data,
                "printer_id": job.printer_id or printer_id,
            }
            for job in jobs
        ]
        return request.make_response(
            json.dumps({"printer_id": printer_id, "jobs": payload}),
            headers=[("Content-Type", "application/json")],
        )

    @http.route("/print-agent/complete", type="http", auth="public", methods=["POST"], csrf=False)
    def complete(self, **kwargs):
        if not self._is_authorized():
            return Response("Unauthorized", status=401)
        
        try:
            payload = json.loads(request.httprequest.data)
        except Exception:
            return Response("Invalid JSON", status=400)

        job_id = payload.get("job_id")
        success = payload.get("success", False)
        error_message = payload.get("error_message")

        job = request.env["print.job"].sudo().browse(job_id)
        if not job:
            return Response("Job not found", status=404)

        from odoo.fields import Datetime

        vals = {
            "state": "completed" if success else "failed",
            "error_message": error_message or False,
            "completed_at": Datetime.now(),
        }
        job.write(vals)

        if success and job.order_id:
            # Mark order shipped if all jobs completed
            remaining = job.order_id.print_job_ids.filtered(lambda j: j.state != "completed")
            if not remaining:
                job.order_id.write({"state": "shipped"})
                try:
                    api = ShopifyAPI.from_env(request.env)
                    shipment = job.shipment_id
                    if shipment:
                        api.create_fulfillment(
                            job.order_id,
                            {
                                "tracking_number": shipment.tracking_number,
                                "tracking_url": shipment.tracking_url,
                            },
                        )
                except Exception as exc:  # pylint: disable=broad-except
                    # Do not block completion if Shopify call fails
                    request.env["ir.logging"].sudo().create(
                        {
                            "name": "print_agent_complete",
                            "type": "server",
                            "level": "ERROR",
                            "dbname": request.env.cr.dbname,
                            "message": f"Fulfillment creation failed: {exc}",
                            "path": __name__,
                            "line": "0",
                            "func": "complete",
                        }
                    )

        return request.make_response(
            json.dumps({"status": "ok"}),
            headers=[("Content-Type", "application/json")],
        )

    @staticmethod
    def _is_authorized() -> bool:
        api_key = request.httprequest.headers.get("Authorization", "")
        if api_key.startswith("Bearer "):
            api_key = api_key.replace("Bearer ", "", 1)
        configured = request.env["ir.config_parameter"].sudo().get_param("print_agent.api_key")
        return configured and api_key and api_key == configured


