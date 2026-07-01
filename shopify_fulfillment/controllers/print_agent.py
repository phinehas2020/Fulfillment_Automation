import json
from datetime import timedelta

from odoo import fields, http
from odoo.http import request, Response

from ..services.alert_service import AlertService
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
            # Create Project Task (Standard To-Do)
            job.order_id.sudo().ensure_fulfillment_task()

            # Mark order shipped if all jobs completed
            remaining = job.order_id.print_job_ids.filtered(lambda j: j.state != "completed")
            if not remaining:
                job.order_id.write({"state": "shipped"})
                try:
                    self._push_fulfillments_to_shopify(job.order_id)
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
                    AlertService.from_env(request.env).notify_error(
                        title="Shopify Fulfillment Push Failed",
                        message=str(exc),
                        order=job.order_id,
                        extra={
                            "print_job_id": str(job.id),
                            "shipment_id": str(job.shipment_id.id if job.shipment_id else ""),
                            "endpoint": "/print-agent/complete",
                        },
                    )

        return request.make_response(
            json.dumps({"status": "ok"}),
            headers=[("Content-Type", "application/json")],
        )

    def _push_fulfillments_to_shopify(self, order):
        """Create a Shopify fulfillment for every shipment with tracking.

        Multi-box orders get one fulfillment per box, scoped to that box's
        line items, so every tracking number reaches the customer instead of
        only the box whose label happened to print last.
        """
        if order.shipment_group_id:
            shipments = order.shipment_group_id.shipment_ids
        else:
            shipments = order.shipment_id

        shipments = shipments.filtered(
            lambda s: s.tracking_number and not s.shopify_fulfillment_id
        )
        if not shipments:
            return

        api = ShopifyAPI.from_env(request.env)
        multi_box = len(shipments) > 1
        for shipment in shipments.sorted("sequence"):
            line_items = self._shipment_line_items(shipment) if multi_box else None
            resp = api.create_fulfillment(
                order,
                {
                    "tracking_number": shipment.tracking_number,
                    "tracking_url": shipment.tracking_url,
                    "carrier": shipment.carrier,
                },
                line_items=line_items,
            )
            fulfillment = resp.get("fulfillment") if isinstance(resp, dict) else None
            if fulfillment and fulfillment.get("id"):
                shipment.write({"shopify_fulfillment_id": fulfillment["id"]})

    @staticmethod
    def _shipment_line_items(shipment):
        """Build [{shopify_line_id, quantity}] for one box's contents."""
        quantities = {}
        if shipment.line_quantities:
            try:
                quantities = {
                    int(line_id): int(quantity)
                    for line_id, quantity in json.loads(shipment.line_quantities).items()
                }
            except (TypeError, ValueError):
                quantities = {}

        items = []
        for line in shipment.line_ids:
            if not line.shopify_line_id:
                continue
            items.append(
                {
                    "shopify_line_id": line.shopify_line_id,
                    "quantity": quantities.get(line.id, line.quantity or 1),
                }
            )
        return items

    @staticmethod
    def _is_authorized() -> bool:
        api_key = request.httprequest.headers.get("Authorization", "")
        if api_key.startswith("Bearer "):
            api_key = api_key.replace("Bearer ", "", 1)
        configured = request.env["ir.config_parameter"].sudo().get_param("print_agent.api_key")
        return configured and api_key and api_key == configured
