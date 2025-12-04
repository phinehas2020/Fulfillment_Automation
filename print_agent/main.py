"""Entry point for Raspberry Pi print agent (skeleton)."""

import time

from config import ODOO_API_KEY, ODOO_URL, POLL_INTERVAL, PRINTER_ID
from odoo_client import OdooClient
from printer import Printer


def main():
    client = OdooClient(base_url=ODOO_URL, api_key=ODOO_API_KEY, printer_id=PRINTER_ID)
    printer = Printer()

    while True:
        jobs = client.fetch_pending_jobs()
        for job in jobs:
            try:
                printer.send_zpl(job.get("zpl_data", ""))
                client.mark_complete(job_id=job.get("id"), success=True)
            except Exception as exc:  # pylint: disable=broad-except
                client.mark_complete(job_id=job.get("id"), success=False, error=str(exc))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()


