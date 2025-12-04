from odoo import models


class PrintJob(models.Model):
    """Queue of pending print jobs for Raspberry Pi agent."""

    _name = "print.job"
    _description = "Print Job"

    # TODO: add fields per specification (job_type, zpl_data, state, etc.)


