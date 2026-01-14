import json
from datetime import timedelta

from odoo import fields, http
from odoo.http import request, Response

from ..services.shopify_api import ShopifyAPI


class PrintAgentController(http.Controller):
    """Endpoints for Raspberry Pi print agent polling + job completion."""

    @staticmethod
    def _get_print_agent_limits():
        ICP = request.env["ir.config_parameter"].sudo()
        max_attempts_raw = ICP.get_param("print_agent.max_attempts", "3")
        lease_seconds_raw = ICP.get_param("print_agent.lease_seconds", "300")
        try:
            max_attempts = int(max_attempts_raw)
        except (TypeError, ValueError):
            max_attempts = 3
        try:
            lease_seconds = int(lease_seconds_raw)
        except (TypeError, ValueError):
            lease_seconds = 300
        return max_attempts, lease_seconds

    def _requeue_stale_jobs(self):
        max_attempts, lease_seconds = self._get_print_agent_limits()
        cutoff = fields.Datetime.now() - timedelta(seconds=lease_seconds)
        cutoff_str = fields.Datetime.to_string(cutoff)

        stale_jobs = request.env["print.job"].sudo().search(
            [("state", "=", "printing"), ("write_date", "<", cutoff_str)]
        )
        for job in stale_jobs:
            attempts = job.attempts or 0
            if attempts >= max_attempts:
                job.write(
                    {
                        "state": "failed",
                        "error_message": "Print lease expired; max attempts reached.",
                        "completed_at": fields.Datetime.now(),
                    }
                )
            else:
                job.write(
                    {
                        "state": "pending",
                        "error_message": "Print lease expired; requeued.",
                        "completed_at": False,
                    }
                )

    @http.route("/print-agent/poll", type="http", auth="public", methods=["GET"], csrf=False)
    def poll(self, printer_id=None, **kwargs):
        if not self._is_authorized():
            return Response("Unauthorized", status=401)

        self._requeue_stale_jobs()

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

        if not job_id:
            return Response("Job id required", status=400)

        job = request.env["print.job"].sudo().browse(job_id).exists()
        if not job:
            return Response("Job not found", status=404)

        max_attempts, _ = self._get_print_agent_limits()
        attempts = job.attempts or 0

        if success:
            vals = {
                "state": "completed",
                "error_message": False,
                "completed_at": fields.Datetime.now(),
            }
        elif attempts >= max_attempts:
            vals = {
                "state": "failed",
                "error_message": error_message or "Print failed; max attempts reached.",
                "completed_at": fields.Datetime.now(),
            }
        else:
            vals = {
                "state": "pending",
                "error_message": error_message or "Print failed; requeued.",
                "completed_at": False,
            }
        job.write(vals)

        if success and job.order_id:
            # Create Fulfillment To-Do if it doesn't exist yet for this order
            todo_model = request.env["fulfillment.todo"].sudo()
            existing_todo = todo_model.search([("order_id", "=", job.order_id.id)], limit=1)
            if not existing_todo:
                ICP = request.env["ir.config_parameter"].sudo()
                default_user_id_raw = ICP.get_param("fulfillment.default_user_id")
                default_user_id = int(default_user_id_raw) if default_user_id_raw else False
                
                todo_vals = {
                    "order_id": job.order_id.id,
                    "user_id": default_user_id,
                    "line_ids": [
                        (0, 0, {
                            "sku": line.sku,
                            "title": line.title,
                            "quantity": line.quantity
                        }) for line in job.order_id.line_ids if line.requires_shipping
                    ]
                }
                new_todo = todo_model.create(todo_vals)
                
                # Create Notification Activity
                if default_user_id:
                    try:
                        new_todo.activity_schedule(
                            'mail.mail_activity_data_todo',
                            summary=f"Pack Order {job.order_id.order_name or job.order_id.order_number}",
                            user_id=default_user_id
                        )
                    except Exception as e:
                        request.env["ir.logging"].sudo().create({
                            "name": "fulfillment_todo_activity",
                            "type": "server",
                            "level": "WARNING",
                            "dbname": request.env.cr.dbname,
                            "message": f"Failed to create activity: {e}",
                            "path": __name__,
                            "line": "0",
                            "func": "complete",
                        })

            # Mark order shipped if all jobs completed
            remaining = job.order_id.print_job_ids.filtered(lambda j: j.state != "completed")
            if not remaining:
                job.order_id.write({"state": "shipped"})
                try:
                    api = ShopifyAPI.from_env(request.env)
                    shipment = job.shipment_id
                    if shipment and not shipment.shopify_fulfillment_id and shipment.tracking_number:
                        resp = api.create_fulfillment(
                            job.order_id,
                            {
                                "tracking_number": shipment.tracking_number,
                                "tracking_url": shipment.tracking_url,
                                "carrier": shipment.carrier,
                            },
                        )
                        fulfillment = resp.get("fulfillment") if isinstance(resp, dict) else None
                        if fulfillment and fulfillment.get("id"):
                            shipment.write({"shopify_fulfillment_id": fulfillment["id"]})
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
