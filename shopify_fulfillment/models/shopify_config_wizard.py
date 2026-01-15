from odoo import fields, models, api


class ShopifyConfigWizard(models.TransientModel):
    _name = 'shopify.config.wizard'
    _description = 'Shopify Fulfillment Configuration'

    # Shopify Settings
    shopify_shop_domain = fields.Char(string="Shopify Shop Domain")
    shopify_api_key = fields.Char(string="Shopify Access Token")
    shopify_webhook_secret = fields.Char(string="Webhook Secret")

    # Shippo Settings
    shippo_api_key = fields.Char(string="Shippo API Token")
    shipper_phone = fields.Char(string="Shipper Phone Number")

    # Print Agent Settings
    print_agent_api_key = fields.Char(string="Print Agent API Key")
    print_agent_max_attempts = fields.Integer(string="Max Attempts")
    print_agent_lease_seconds = fields.Integer(string="Lease Seconds")

    # Automation
    fulfillment_auto_process = fields.Boolean(string="Auto-Process Orders")
    fulfillment_default_user_id = fields.Many2one('res.users', string="Default Fulfillment Employee")
    fulfillment_stock_location_id = fields.Many2one('stock.location', string="Source Stock Location")

    @api.model
    def default_get(self, fields_list):
        """Load current values from ir.config_parameter."""
        res = super().default_get(fields_list)
        ICP = self.env['ir.config_parameter'].sudo()
        res.update({
            'shopify_shop_domain': ICP.get_param('shopify.shop_domain', ''),
            'shopify_api_key': ICP.get_param('shopify.api_key', ''),
            'shopify_webhook_secret': ICP.get_param('shopify.webhook_secret', ''),
            'shippo_api_key': ICP.get_param('shippo.api_key', ''),
            'shipper_phone': ICP.get_param('shippo.shipper_phone', '555-555-5555'),
            'print_agent_api_key': ICP.get_param('print_agent.api_key', ''),
            'print_agent_max_attempts': int(ICP.get_param('print_agent.max_attempts', '3') or 3),
            'print_agent_lease_seconds': int(ICP.get_param('print_agent.lease_seconds', '300') or 300),
            'fulfillment_auto_process': ICP.get_param('fulfillment.auto_process', 'False') == 'True',
            'fulfillment_default_user_id': int(ICP.get_param('fulfillment.default_user_id', '0') or 0) or False,
            'fulfillment_stock_location_id': int(ICP.get_param('fulfillment.stock_location_id', '0') or 0) or False,
        })
        return res

    def action_save(self):
        """Save values to ir.config_parameter."""
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('shopify.shop_domain', self.shopify_shop_domain or '')
        ICP.set_param('shopify.api_key', self.shopify_api_key or '')
        ICP.set_param('shopify.webhook_secret', self.shopify_webhook_secret or '')
        ICP.set_param('shippo.api_key', self.shippo_api_key or '')
        ICP.set_param('shippo.shipper_phone', self.shipper_phone or '')
        ICP.set_param('print_agent.api_key', self.print_agent_api_key or '')
        ICP.set_param('print_agent.max_attempts', str(self.print_agent_max_attempts or 3))
        ICP.set_param('print_agent.lease_seconds', str(self.print_agent_lease_seconds or 300))
        ICP.set_param('fulfillment.auto_process', str(self.fulfillment_auto_process))
        ICP.set_param('fulfillment.default_user_id', str(self.fulfillment_default_user_id.id or ''))
        ICP.set_param('fulfillment.stock_location_id', str(self.fulfillment_stock_location_id.id or ''))
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': 'Configuration saved successfully!',
                'type': 'success',
                'sticky': False,
            }
        }
