# Raspberry Pi Print Agent (skeleton)

- Polls Odoo for pending print jobs and sends ZPL to the Zebra ZP 505.
- Reports completion/failure back to Odoo.

Files
- `main.py`: polling loop entrypoint.
- `config.py`: configuration placeholders.
- `odoo_client.py`: HTTP client stub.
- `printer.py`: USB printer wrapper stub.
- `print_agent.service`: systemd unit template.
- `requirements.txt`: dependencies (requests).

Implementation is intentionally minimal; fill in real logic per `Shopify_Fulfillment_Automation_Spec.txt`.


