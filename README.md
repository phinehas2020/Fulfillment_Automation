# Shopify Fulfillment Automation

Skeleton project for the Odoo 18 fulfillment module and Raspberry Pi print agent described in `Shopify_Fulfillment_Automation_Spec.txt`.

Current state: end-to-end skeleton with models, controllers, services, views, and print agent wiring. Shopify API calls are stubbed and must be wired to real endpoints/credentials before production.

Contents
- `shopify_fulfillment/`: Odoo module scaffold (models, controllers, services, views, security, data, static).
- `print_agent/`: Raspberry Pi print agent scaffold (polling loop, Odoo client, printer wrapper, systemd service).
- `Shopify_Fulfillment_Automation_Spec.txt`: Full project specification converted from the provided RTF.

Quick setup
1) In Odoo, set system parameters:
   - `shopify.shop_domain`, `shopify.api_key`, `shopify.api_version`, `shopify.webhook_secret`
   - `print_agent.api_key` (shared with the Pi)
2) Install the module (`shopify_fulfillment`) and confirm default boxes load.
3) Configure Shopify webhook (orders/create) to `https://<odoo>/shopify/webhook/order`.
4) On the Pi, set `config.py` (ODOO_URL, ODOO_API_KEY, PRINTER_PATH, etc.), install `requests`, and enable `print_agent.service`.

Notes
- Shopify rate/label calls are placeholder stubs; replace with real Shipping API endpoints.
- ZPL conversion is simplified; adjust to request ZPL from Shopify or convert PDFs.
- Box selection uses volume/weight heuristics per the spec with seeded default boxes.
- Print agent polls `/print-agent/poll` and reports results to `/print-agent/complete`.


