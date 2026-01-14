from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    shopify_shop_domain = fields.Char(config_parameter='shopify.shop_domain', string="Shopify Shop Domain")
    shopify_api_key = fields.Char(config_parameter='shopify.api_key', string="Shopify Access Token")
    shopify_webhook_secret = fields.Char(config_parameter='shopify.webhook_secret', string="Webhook Secret")
    shippo_api_key = fields.Char(config_parameter='shippo.api_key', string="Shippo API Token")
    shipper_phone = fields.Char(config_parameter='shippo.shipper_phone', string="Shipper Phone Number", default="555-555-5555")
    fulfillment_auto_process = fields.Boolean(config_parameter='fulfillment.auto_process', string="Auto-Process Orders")
    print_agent_api_key = fields.Char(config_parameter='print_agent.api_key', string="Print Agent API Key")
    print_agent_max_attempts = fields.Integer(
        config_parameter='print_agent.max_attempts',
        string="Print Agent Max Attempts",
        default=3,
    )
    print_agent_lease_seconds = fields.Integer(
        config_parameter='print_agent.lease_seconds',
        string="Print Agent Lease Seconds",
        default=300,
    )
