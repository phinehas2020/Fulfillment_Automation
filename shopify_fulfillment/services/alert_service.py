import html
import logging
from typing import Dict, Iterable, Optional

import requests


_logger = logging.getLogger(__name__)


class AlertService:
    """Send immediate fulfillment error alerts via email and/or Teams webhook."""

    def __init__(self, env):
        self.env = env
        self.icp = env["ir.config_parameter"].sudo()

    @classmethod
    def from_env(cls, env):
        return cls(env)

    def notify_error(
        self,
        *,
        title: str,
        message: str,
        order=None,
        extra: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Notify configured channels. Returns True when at least one channel succeeds."""
        subject = f"[Fulfillment Error] {title or 'Unknown error'}"
        body_text = self._build_body_text(title=title, message=message, order=order, extra=extra)
        body_html = "<pre>%s</pre>" % html.escape(body_text)

        email_ok = self._send_email(subject=subject, body_html=body_html)
        teams_ok = self._send_teams(subject=subject, body_text=body_text)
        if not email_ok and not teams_ok:
            _logger.warning("AlertService: no alert channels succeeded for '%s'", subject)
        return email_ok or teams_ok

    def _build_body_text(
        self,
        *,
        title: str,
        message: str,
        order=None,
        extra: Optional[Dict[str, str]] = None,
    ) -> str:
        lines = [
            f"Title: {title or 'Unknown error'}",
            f"Database: {self.env.cr.dbname}",
            f"Company: {self.env.company.display_name}",
            f"Message: {message or 'No message provided'}",
        ]
        if order:
            lines.extend(
                [
                    f"Order ID: {order.id}",
                    f"Order Name: {order.order_name or order.order_number or '-'}",
                    f"Shopify ID: {order.shopify_id or '-'}",
                    f"Customer: {order.customer_name or '-'}",
                    f"Requested Shipping: {order.requested_shipping_method or '-'}",
                    f"State: {order.state or '-'}",
                ]
            )
        if extra:
            for key in sorted(extra):
                lines.append(f"{key}: {extra[key]}")
        return "\n".join(lines)

    def _recipient_emails(self) -> Iterable[str]:
        raw = self.icp.get_param("fulfillment.error_alert_emails", "") or ""
        chunks = raw.replace(";", ",").replace("\n", ",").split(",")
        recipients = [item.strip() for item in chunks if item.strip()]

        reviewer_id = self.icp.get_param("fulfillment.risk_reviewer_id")
        if reviewer_id and reviewer_id.isdigit():
            user = self.env["res.users"].sudo().browse(int(reviewer_id)).exists()
            reviewer_email = user.partner_id.email if user else False
            if reviewer_email:
                recipients.append(reviewer_email.strip())

        # Preserve order, remove duplicates.
        deduped = []
        seen = set()
        for recipient in recipients:
            if recipient not in seen:
                deduped.append(recipient)
                seen.add(recipient)
        return deduped

    def _send_email(self, *, subject: str, body_html: str) -> bool:
        recipients = list(self._recipient_emails())
        if not recipients:
            return False

        sender = (
            self.env.company.email
            or self.env.user.email
            or "no-reply@localhost"
        )
        try:
            mail = self.env["mail.mail"].sudo().create(
                {
                    "subject": subject,
                    "body_html": body_html,
                    "email_to": ",".join(recipients),
                    "email_from": sender,
                    "auto_delete": True,
                }
            )
            mail.send()
            return True
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception("AlertService: email send failed: %s", exc)
            return False

    def _send_teams(self, *, subject: str, body_text: str) -> bool:
        webhook_url = self.icp.get_param("fulfillment.error_alert_teams_webhook_url", "") or ""
        webhook_url = webhook_url.strip()
        if not webhook_url:
            return False

        payload = {
            "text": f"{subject}\n\n{body_text}",
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code >= 400:
                _logger.error(
                    "AlertService: teams webhook failed status=%s body=%s",
                    resp.status_code,
                    (resp.text or "")[:500],
                )
                return False
            return True
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception("AlertService: teams webhook send failed: %s", exc)
            return False
