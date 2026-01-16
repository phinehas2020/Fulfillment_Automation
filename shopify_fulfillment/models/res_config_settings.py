from odoo import fields, models, api

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
    
    # Many2one fields don't support config_parameter directly in Odoo safely.
    # We use manual get/set for these.
    fulfillment_default_user_id = fields.Many2one(
        'res.users', 
        string="Default Fulfillment Employee"
    )
    fulfillment_stock_location_id = fields.Many2one(
        'stock.location', 
        string="Source Stock Location",
        domain=[('usage', '=', 'internal')]
    )
    fulfillment_risk_reviewer_id = fields.Many2one(
        'res.users',
        string="Risk Reviewer (Email Notification)",
        help="Employee to notify when an order is flagged as high risk or address issues."
    )

    def set_values(self):
        super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('fulfillment.default_user_id', str(self.fulfillment_default_user_id.id or ''))
        ICP.set_param('fulfillment.stock_location_id', str(self.fulfillment_stock_location_id.id or ''))
        ICP.set_param('fulfillment.risk_reviewer_id', str(self.fulfillment_risk_reviewer_id.id or ''))

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        
        def _get_int(key):
            val = ICP.get_param(key)
            try:
                return int(val) if val else False
            except ValueError:
                return False

        res.update({
            'fulfillment_default_user_id': _get_int('fulfillment.default_user_id'),
            'fulfillment_stock_location_id': _get_int('fulfillment.stock_location_id'),
            'fulfillment_risk_reviewer_id': _get_int('fulfillment.risk_reviewer_id'),
        })
        return res
