# Shopify Fulfillment Automation

Skeleton project for the Odoo 18 fulfillment module and Raspberry Pi print agent described in `Shopify_Fulfillment_Automation_Spec.txt`.

Current state: directory and file scaffolding only. Business logic, views, and integrations are intentionally stubbed for follow-up implementation.

Contents
- `shopify_fulfillment/`: Odoo module scaffold (models, controllers, services, views, security, data, static).
- `print_agent/`: Raspberry Pi print agent scaffold (polling loop, Odoo client, printer wrapper, systemd service).
- `Shopify_Fulfillment_Automation_Spec.txt`: Full project specification converted from the provided RTF.

Next steps (per spec)
- Implement Odoo models, controllers, and services.
- Build Shopify API integration, box selector, ZPL generation, and print queue.
- Flesh out XML views, security rules, and data seeds.
- Implement print agent polling, printer I/O, and completion callbacks.
- Add tests and deployment instructions.


