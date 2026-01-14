from odoo import fields, models, api


class ShopifyFulfillmentConfig(models.Model):
    _name = 'shopify.fulfillment.config'
    _description = 'Shopify Fulfillment Configuration'
    _rec_name = 'id'

    # Ensure only one config record exists
    @api.model
    def _get_config(self):
        config = self.search([], limit=1)
        if not config:
            config = self.create({})
        return config

    # Shopify Settings
    shopify_shop_domain = fields.Char(string="Shopify Shop Domain", help="e.g. mystore.myshopify.com")
    shopify_api_key = fields.Char(string="Shopify Access Token")
    shopify_webhook_secret = fields.Char(string="Webhook Secret")

    # Shippo Settings
    shippo_api_key = fields.Char(string="Shippo API Token")
    shipper_phone = fields.Char(string="Shipper Phone Number", default="555-555-5555")

    # Print Agent Settings
    print_agent_api_key = fields.Char(string="Print Agent API Key")
    print_agent_max_attempts = fields.Integer(string="Max Attempts", default=3)
    print_agent_lease_seconds = fields.Integer(string="Lease Seconds", default=300)

    # Automation
    fulfillment_auto_process = fields.Boolean(string="Auto-Process Orders", default=False)

    def write(self, vals):
        """Sync values to ir.config_parameter for backward compatibility"""
        res = super().write(vals)
        ICP = self.env['ir.config_parameter'].sudo()
        param_map = {
            'shopify_shop_domain': 'shopify.shop_domain',
            'shopify_api_key': 'shopify.api_key',
            'shopify_webhook_secret': 'shopify.webhook_secret',
            'shippo_api_key': 'shippo.api_key',
            'shipper_phone': 'shippo.shipper_phone',
            'print_agent_api_key': 'print_agent.api_key',
            'print_agent_max_attempts': 'print_agent.max_attempts',
            'print_agent_lease_seconds': 'print_agent.lease_seconds',
            'fulfillment_auto_process': 'fulfillment.auto_process',
        }
        for field_name, param_name in param_map.items():
            if field_name in vals:
                ICP.set_param(param_name, vals[field_name])
        return res

    @api.model
    def create(self, vals):
        """Sync values to ir.config_parameter on create"""
        record = super().create(vals)
        record.write(vals)  # Trigger the param sync
        return record
