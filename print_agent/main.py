"""Entry point for Raspberry Pi print agent (skeleton)."""

import time

from config import ODOO_API_KEY, ODOO_URL, POLL_INTERVAL, PRINTER_ID
from odoo_client import OdooClient
from printer import Printer


def main():
    client = OdooClient(base_url=ODOO_URL, api_key=ODOO_API_KEY, printer_id=PRINTER_ID)
    printer = Printer()

    while True:
        # TODO: fetch jobs and send to printer; report completion/errors
        jobs = client.fetch_pending_jobs()
        for job in jobs:
            printer.send_zpl(job.get("zpl_data", ""))
            client.mark_complete(job_id=job.get("id"), success=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()


