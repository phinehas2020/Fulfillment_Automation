import logging

from odoo import fields, models


_logger = logging.getLogger(__name__)


class PrintJob(models.Model):
    """Queue of pending print jobs for Raspberry Pi agent."""

    _name = "print.job"
    _description = "Print Job"

    order_id = fields.Many2one("shopify.order", ondelete="cascade")
    shipment_id = fields.Many2one("fulfillment.shipment", ondelete="set null")
    job_type = fields.Selection(
        [
            ("label", "Label"),
            ("label_pdf", "Label (PDF)"),
            ("packing_slip", "Packing Slip"),
        ],
        default="label",
    )
    zpl_data = fields.Text()
    state = fields.Selection(
        [("pending", "Pending"), ("printing", "Printing"), ("completed", "Completed"), ("failed", "Failed")],
        default="pending",
    )
    printer_id = fields.Char()
    attempts = fields.Integer(default=0)
    error_message = fields.Text()
    created_at = fields.Datetime(default=fields.Datetime.now)
    completed_at = fields.Datetime()

    def action_retry(self):
        """Reset failed jobs to pending."""
        for job in self:
            job.write({
                "state": "pending",
                "attempts": 0,
                "error_message": False
            })

    def _send_failed_print_alert(self, message: str):
        self.ensure_one()
        try:
            from odoo.addons.shopify_fulfillment.services.alert_service import AlertService

            order = self.order_id if self.order_id else None
            AlertService.from_env(self.env).notify_error(
                title="Print Job Failed",
                message=message,
                order=order,
                extra={
                    "print_job_id": str(self.id),
                    "attempts": str(self.attempts or 0),
                },
            )
        except Exception:
            # Never block print queue writes due to alert transport errors.
            _logger.exception("Print job %s: failed to send failed-print alert", self.id)

    def write(self, vals):
        tracked = None
        if vals.get("state") == "failed":
            tracked = {job.id: job.state for job in self}

        res = super().write(vals)

        if tracked:
            alert_message = vals.get("error_message")
            for job in self:
                if tracked.get(job.id) == "failed":
                    continue
                message = alert_message or job.error_message or "Print job failed."
                job._send_failed_print_alert(message)

        return res
