"""Entry point for Raspberry Pi print agent (skeleton)."""

import time

from config import ODOO_API_KEY, ODOO_URL, POLL_INTERVAL, PRINTER_ID
from odoo_client import OdooClient
from printer import Printer


def main():
    client = OdooClient(base_url=ODOO_URL, api_key=ODOO_API_KEY, printer_id=PRINTER_ID)
    printer = Printer()

    print(f"Starting Print Agent for printer: {PRINTER_ID}")
    print(f"Connecting to Odoo at: {ODOO_URL}")

    while True:
        try:
            jobs = client.fetch_pending_jobs()
            if jobs:
                print(f"Found {len(jobs)} jobs.")
            
            for job in jobs:
                print(f"Processing job {job.get('id')}...")
                try:
                    printer.send_zpl(job.get("zpl_data", ""))
                    client.mark_complete(job_id=job.get("id"), success=True)
                    print(f"Job {job.get('id')} completed successfully.")
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"Job {job.get('id')} failed: {exc}")
                    client.mark_complete(job_id=job.get("id"), success=False, error=str(exc))
        except Exception as e:
            print(f"Error polling Odoo: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()


