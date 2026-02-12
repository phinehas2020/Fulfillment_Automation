import logging
from datetime import datetime, timezone

from odoo import api, exceptions, fields, models

from ..services.shippo_service import ShippoService

_logger = logging.getLogger(__name__)


class ShippoRecentTransaction(models.TransientModel):
    _name = "shippo.recent.transaction"
    _description = "Shippo Recent Transaction"
    _order = "transaction_date desc, id desc"

    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True, index=True)
    shippo_object_id = fields.Char(string="Shippo Transaction ID", readonly=True)
    tracking_number = fields.Char(readonly=True)
    tracking_url = fields.Char(readonly=True)
    label_url = fields.Char(readonly=True)
    label_file_type = fields.Char(readonly=True)
    status = fields.Char(readonly=True)
    transaction_date = fields.Datetime(readonly=True)
    carrier = fields.Char(readonly=True)
    service = fields.Char(readonly=True)
    local_shipment_id = fields.Many2one("fulfillment.shipment", readonly=True)
    order_id = fields.Many2one("shopify.order", readonly=True)
    has_local_zpl = fields.Boolean(readonly=True)

    @api.model
    def action_fetch_recent(self):
        shippo = ShippoService.from_env(self.env)
        if not shippo:
            raise exceptions.UserError("Shippo API key is not configured. Please update Configuration first.")

        transactions = shippo.get_recent_transactions(limit=20)

        current_user_records = self.sudo().search([("user_id", "=", self.env.uid)])
        if current_user_records:
            current_user_records.unlink()

        tracking_numbers = [
            transaction.get("tracking_number")
            for transaction in transactions
            if transaction.get("tracking_number")
        ]

        local_by_tracking = {}
        if tracking_numbers:
            local_shipments = self.env["fulfillment.shipment"].search(
                [("tracking_number", "in", tracking_numbers)],
                order="id desc",
            )
            for shipment in local_shipments:
                if shipment.tracking_number not in local_by_tracking:
                    local_by_tracking[shipment.tracking_number] = shipment

        rows = []
        for transaction in transactions:
            tracking_number = transaction.get("tracking_number")
            local_shipment = local_by_tracking.get(tracking_number)

            carrier, service = self._extract_carrier_service(transaction)
            if local_shipment:
                carrier = carrier or local_shipment.carrier
                service = service or local_shipment.service

            transaction_date = self._parse_shippo_datetime(transaction.get("object_created"))
            order_id = local_shipment.order_id.id if local_shipment and local_shipment.order_id else False

            rows.append(
                {
                    "user_id": self.env.uid,
                    "shippo_object_id": transaction.get("object_id"),
                    "tracking_number": tracking_number,
                    "tracking_url": transaction.get("tracking_url_provider") or transaction.get("tracking_url"),
                    "label_url": transaction.get("label_url"),
                    "label_file_type": transaction.get("label_file_type"),
                    "status": transaction.get("status"),
                    "transaction_date": transaction_date,
                    "carrier": carrier,
                    "service": service,
                    "local_shipment_id": local_shipment.id if local_shipment else False,
                    "order_id": order_id,
                    "has_local_zpl": bool(local_shipment and local_shipment.label_zpl),
                }
            )

        if rows:
            self.create(rows)

        action = self.env.ref("shopify_fulfillment.action_recent_shipments").read()[0]
        action["domain"] = [("user_id", "=", self.env.uid)]
        return action

    def action_reprint_label(self):
        self.ensure_one()

        zpl_data = False
        pdf_data = False
        job_type = "label"
        shipment = self.local_shipment_id.sudo() if self.local_shipment_id else self.env["fulfillment.shipment"]
        order = shipment.order_id if shipment else self.order_id

        if shipment and shipment.label_zpl:
            if self._looks_like_zpl(shipment.label_zpl):
                zpl_data = shipment.label_zpl
            elif self._looks_like_pdf(shipment.label_zpl):
                pdf_data = shipment.label_zpl
                job_type = "label_pdf"

        if not zpl_data and not pdf_data and self.label_url:
            shippo = ShippoService.from_env(self.env)
            if not shippo:
                raise exceptions.UserError(
                    "No local ZPL found and Shippo API key is not configured to download label content."
                )
            downloaded_data = shippo._download_url(self.label_url)

            if not downloaded_data:
                raise exceptions.UserError("Unable to retrieve label data for reprint.")

            if self._looks_like_zpl(downloaded_data):
                zpl_data = downloaded_data
            elif self._looks_like_pdf(downloaded_data):
                pdf_data = downloaded_data
                job_type = "label_pdf"
            else:
                raise exceptions.UserError(
                    "Downloaded label content is not a supported format for reprint (expected ZPL or PDF)."
                )

        if not zpl_data and not pdf_data:
            raise exceptions.UserError("Unable to retrieve label data for reprint.")

        self.env["print.job"].create(
            {
                "order_id": order.id if order else False,
                "shipment_id": shipment.id if shipment else False,
                "job_type": job_type,
                "zpl_data": zpl_data or pdf_data,
                "state": "pending",
                "printer_id": False,
            }
        )

        tracking_label = self.tracking_number or "shipment"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Reprint Queued",
                "message": f"A label reprint was queued for {tracking_label}.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def _extract_carrier_service(self, transaction):
        carrier = transaction.get("carrier")
        service = transaction.get("service")

        rate = transaction.get("rate")
        if isinstance(rate, dict):
            carrier = carrier or rate.get("provider")
            service = service or rate.get("servicelevel_name")
            rate_servicelevel = rate.get("servicelevel")
            if isinstance(rate_servicelevel, dict):
                service = service or rate_servicelevel.get("name")

        servicelevel = transaction.get("servicelevel")
        if isinstance(servicelevel, dict):
            service = service or servicelevel.get("name")

        return carrier, service

    @api.model
    def _parse_shippo_datetime(self, value):
        if not value:
            return False

        try:
            normalized = value
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"

            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except Exception:
            _logger.warning("Could not parse Shippo transaction date: %s", value)
            return False

    @api.model
    def _looks_like_zpl(self, content):
        if not content:
            return False

        normalized = (content or "").lstrip()
        header = normalized[:32]

        if header.startswith("%PDF-"):
            return False

        # Require real ZPL framing markers in sequence.
        if not normalized.startswith("^XA"):
            return False

        return "^XZ" in normalized[:4096]

    @api.model
    def _looks_like_pdf(self, content):
        if not content:
            return False
        return (content or "").lstrip().startswith("%PDF-")
