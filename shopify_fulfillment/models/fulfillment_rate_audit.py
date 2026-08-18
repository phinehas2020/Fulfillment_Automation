import json
from datetime import timezone

from odoo import api, fields, models


class FulfillmentRateAudit(models.Model):
    """Audit row per purchased label capturing the top 3 cheapest rates.

    One row is written each time a label is successfully purchased, so the
    operator can review weekly whether the selected rate was optimal.
    """

    _name = "fulfillment.rate.audit"
    _description = "Fulfillment Rate Audit"
    _order = "purchased_at desc, id desc"

    order_id = fields.Many2one("shopify.order", string="Order", index=True, ondelete="set null")
    shipment_id = fields.Many2one("fulfillment.shipment", string="Shipment", ondelete="set null")
    group_id = fields.Many2one("fulfillment.shipment.group", string="Shipment Group", ondelete="set null")
    purchased_at = fields.Datetime(string="Purchased At", index=True)

    box_sequence = fields.Integer(string="Box #")
    weight_grams = fields.Float(string="Weight (g)")
    dest_city = fields.Char(string="Destination City")
    dest_state = fields.Char(string="Destination State")
    dest_zip = fields.Char(string="Destination ZIP")

    is_residential = fields.Selection(
        [("yes", "Residential"), ("no", "Commercial"), ("unknown", "Unknown")],
        string="Address Type",
        default="unknown",
    )

    selected_carrier = fields.Char(string="Selected Carrier")
    selected_service = fields.Char(string="Selected Service")
    selected_amount = fields.Float(string="Selected Rate")
    selected_currency = fields.Char(string="Currency")
    rate_source = fields.Selection(
        [("shippo", "Shippo"), ("amazon_shipping", "Amazon Buy Shipping"), ("mock", "Mock/Test")],
        string="Rate Source",
        default="shippo",
        index=True,
    )
    decision = fields.Selection(
        [("purchased", "Purchased"), ("held", "Held for Review"), ("error", "Error")],
        default="purchased",
        index=True,
    )
    selected_rate_id = fields.Char(string="Selected Rate ID")
    selected_service_token = fields.Char(string="Service Token")
    selected_estimated_days = fields.Integer(string="Estimated Transit Days")
    selected_promised_delivery_at = fields.Datetime(string="Promised Delivery")
    delivery_deadline_at = fields.Datetime(string="Required Delivery By")
    cheapest_eligible_amount = fields.Float(string="Cheapest Eligible Rate")
    policy_version = fields.Char(string="Policy Version")
    selection_reason = fields.Char(string="Selection Reason")
    rejection_summary = fields.Text(string="Rejected Offers")
    candidate_summary_json = fields.Text(string="Candidate Summary JSON")

    rate_1_carrier = fields.Char(string="#1 Carrier")
    rate_1_service = fields.Char(string="#1 Service")
    rate_1_amount = fields.Float(string="#1 Rate")

    rate_2_carrier = fields.Char(string="#2 Carrier")
    rate_2_service = fields.Char(string="#2 Service")
    rate_2_amount = fields.Float(string="#2 Rate")

    rate_3_carrier = fields.Char(string="#3 Carrier")
    rate_3_service = fields.Char(string="#3 Service")
    rate_3_amount = fields.Float(string="#3 Rate")

    delta_vs_cheapest = fields.Float(
        string="Overpaid vs #1",
        compute="_compute_delta_vs_cheapest",
        store=True,
        help="Selected rate minus the cheapest available rate. 0 when the cheapest was selected.",
    )

    @api.depends("selected_amount", "rate_1_amount")
    def _compute_delta_vs_cheapest(self):
        for row in self:
            if row.selected_amount and row.rate_1_amount:
                row.delta_vs_cheapest = row.selected_amount - row.rate_1_amount
            else:
                row.delta_vs_cheapest = 0.0

    @api.model
    def log_purchase(self, *, order, shipment, group, sequence, weight_grams,
                     rates, selected_rate, is_residential, rate_meta=None,
                     selection=None, decision="purchased"):
        """Create an audit row for a completed label purchase.

        Args:
            order: shopify.order record
            shipment: fulfillment.shipment record (just created)
            group: fulfillment.shipment.group record
            sequence: int box sequence
            weight_grams: float total parcel weight
            rates: list of Shippo rate dicts that were considered
            selected_rate: the Shippo rate dict that was actually purchased
            is_residential: bool | None from Shippo validation
        """
        def _amount(rate):
            try:
                return float(rate.get("amount") or 0.0)
            except (TypeError, ValueError):
                return 999999.0

        sorted_rates = sorted(rates or [], key=_amount)
        top3 = sorted_rates[:3]

        def _carrier(rate):
            return (rate or {}).get("provider") or ""

        def _service(rate):
            return ((rate or {}).get("servicelevel") or {}).get("name") or ""

        residential_map = {True: "yes", False: "no"}
        rate_meta = rate_meta or {}
        selection = selection or {}

        def _datetime(value):
            if not value:
                return False
            if hasattr(value, "tzinfo"):
                if value.tzinfo:
                    return value.astimezone(timezone.utc).replace(tzinfo=None)
                return value
            try:
                from dateutil import parser

                parsed = parser.parse(str(value))
                if parsed.tzinfo:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed
            except (TypeError, ValueError, OverflowError):
                return False

        candidate_summary = []
        for rate in rates or []:
            amazon_meta = (rate or {}).get("_amazon") or {}
            candidate_summary.append(
                {
                    "source": (rate or {}).get("_source") or "shippo",
                    "rate_id": (rate or {}).get("object_id") or (rate or {}).get("id"),
                    "carrier": _carrier(rate),
                    "service": _service(rate),
                    "service_token": ((rate or {}).get("servicelevel") or {}).get("token"),
                    "amount": _amount(rate),
                    "currency": (rate or {}).get("currency"),
                    "estimated_days": (rate or {}).get("estimated_days"),
                    "promised_delivery": (rate or {}).get("arrives_by"),
                    "requires_additional_inputs": amazon_meta.get(
                        "requires_additional_inputs"
                    ),
                    "unresolved_required_vas_groups": amazon_meta.get(
                        "unresolved_required_vas_groups"
                    ),
                }
            )

        vals = {
            "order_id": order.id if order else False,
            "shipment_id": shipment.id if shipment else False,
            "group_id": group.id if group else False,
            "purchased_at": fields.Datetime.now(),
            "box_sequence": sequence,
            "weight_grams": weight_grams or 0.0,
            "dest_city": order.shipping_city or "" if order else "",
            "dest_state": order.shipping_state or "" if order else "",
            "dest_zip": order.shipping_zip or "" if order else "",
            "is_residential": residential_map.get(is_residential, "unknown"),
            "selected_carrier": _carrier(selected_rate),
            "selected_service": _service(selected_rate),
            "selected_amount": (
                _amount(selected_rate)
                if selected_rate and decision == "purchased"
                else 0.0
            ),
            "selected_currency": (selected_rate or {}).get("currency") or "",
            "rate_source": (selected_rate or {}).get("_source") or rate_meta.get("provider") or "shippo",
            "decision": decision,
            "selected_rate_id": (selected_rate or {}).get("object_id") or (selected_rate or {}).get("id") or "",
            "selected_service_token": ((selected_rate or {}).get("servicelevel") or {}).get("token") or "",
            "selected_estimated_days": (selected_rate or {}).get("estimated_days") or 0,
            "selected_promised_delivery_at": _datetime((selected_rate or {}).get("arrives_by")),
            "delivery_deadline_at": getattr(order, "amazon_latest_delivery_at", False) if order else False,
            "cheapest_eligible_amount": float(selection.get("cheapest_eligible_amount") or 0.0),
            "policy_version": selection.get("policy_version") or rate_meta.get("policy_version") or "",
            "selection_reason": selection.get("reason") or "",
            "rejection_summary": selection.get("rejection_summary") or "",
            "candidate_summary_json": json.dumps(candidate_summary, sort_keys=True),
            "rate_1_carrier": _carrier(top3[0]) if len(top3) > 0 else "",
            "rate_1_service": _service(top3[0]) if len(top3) > 0 else "",
            "rate_1_amount": _amount(top3[0]) if len(top3) > 0 else 0.0,
            "rate_2_carrier": _carrier(top3[1]) if len(top3) > 1 else "",
            "rate_2_service": _service(top3[1]) if len(top3) > 1 else "",
            "rate_2_amount": _amount(top3[1]) if len(top3) > 1 else 0.0,
            "rate_3_carrier": _carrier(top3[2]) if len(top3) > 2 else "",
            "rate_3_service": _service(top3[2]) if len(top3) > 2 else "",
            "rate_3_amount": _amount(top3[2]) if len(top3) > 2 else 0.0,
        }
        return self.create(vals)
