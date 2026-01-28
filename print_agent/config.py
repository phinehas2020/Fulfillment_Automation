"""Configuration placeholders for the print agent."""

import os

# Load from environment variables or fall back to defaults
ODOO_URL = os.getenv("ODOO_URL", "https://internal.homesteadgristmill.com")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "homestead-printer-2025")
PRINTER_PATH = os.getenv("PRINTER_PATH", "/dev/usb/lp0")
PRINTER_ID = os.getenv("PRINTER_ID", "warehouse-1")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

CUPS_PRINTER_NAME = os.getenv("CUPS_PRINTER_NAME", "ZebraZP505")
USE_CUPS = os.getenv("USE_CUPS", "true").lower() == "true"



