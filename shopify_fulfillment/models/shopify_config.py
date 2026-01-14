from odoo import fields, models, api


class ShopifyFulfillmentConfig(models.Model):
    _name = 'shopify.fulfillment.config'
    _description = 'Shopify Fulfillment Configuration'

    name = fields.Char(string="Name", default="Shopify Fulfillment Settings", readonly=True)
    
    # Shopify Settings
    shopify_shop_domain = fields.Char(string="Shopify Shop Domain")
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

    @api.model
    def get_config(self):
        """Get or create the singleton configuration record."""
        config = self.search([], limit=1)
        if not config:
            config = self.create({'name': 'Shopify Fulfillment Settings'})
        return config
