from odoo import fields, models


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


