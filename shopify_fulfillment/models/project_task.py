from odoo import api, fields, models, _
from odoo.exceptions import UserError

class ProjectTask(models.Model):
    _inherit = "project.task"

    shopify_order_id = fields.Many2one("shopify.order", string="Shopify Order", readonly=True)
    is_fulfillment_task = fields.Boolean(string="Is Fulfillment Task", default=False)
    fulfillment_inventory_deducted = fields.Boolean(string="Inventory Deducted", default=False, readonly=True)

    def action_fulfillment_deduct_inventory(self):
        """Deduct inventory for the linked Shopify Order."""
        self.ensure_one()
        if not self.shopify_order_id or not self.is_fulfillment_task:
            return
            
        if self.fulfillment_inventory_deducted:
            return

        # Get configuration
        ICP = self.env["ir.config_parameter"].sudo()
        location_id_raw = ICP.get_param("fulfillment.stock_location_id")
        if not location_id_raw:
            raise UserError(_("Please configure a Source Stock Location in Shopify Settings first."))
        
        location_id = int(location_id_raw)
        src_location = self.env['stock.location'].browse(location_id)
        if not src_location.exists():
            raise UserError(_("Configured Stock Location not found."))

        # Destination is usually 'Customer' location
        customer_location = self.env.ref('stock.stock_location_customers')

        # Create Stock Picking (Delivery Order)
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('warehouse_id.company_id', '=', self.env.company.id)
        ], limit=1)
        
        if not picking_type:
             raise UserError(_("No Delivery Picking Type found for this company."))

        picking_vals = {
            'picking_type_id': picking_type.id,
            'location_id': src_location.id,
            'location_dest_id': customer_location.id,
            'origin': self.shopify_order_id.order_name or self.shopify_order_id.order_number,
            'move_type': 'direct',
        }
        
        picking = self.env['stock.picking'].create(picking_vals)
        
        moves = []
        # We need to look at the order lines from the linked order
        # Since we don't duplicate them onto the task, we read them from order_id
        for line in self.shopify_order_id.line_ids:
            if not line.requires_shipping:
                continue

            # Match product by SKU (internal_reference)
            product = self.env['product.product'].search([('default_code', '=', line.sku)], limit=1)
            if not product:
                # Fallback: maybe it's on the template?
                product = self.env['product.product'].search([('product_tmpl_id.default_code', '=', line.sku)], limit=1)
            
            if not product:
                self.message_post(body=_("Could not find Odoo product for SKU: %s. Inventory was not decremented for this item.") % line.sku)
                continue

            move_vals = {
                'name': product.name,
                'product_id': product.id,
                'product_uom_qty': line.quantity,
                'product_uom': product.uom_id.id,
                'location_id': src_location.id,
                'location_dest_id': customer_location.id,
                'picking_id': picking.id,
            }
            moves.append(self.env['stock.move'].create(move_vals))

        if not moves:
            picking.unlink()
            # If no moves, maybe we just mark it as done anyway?
            # But let's warn.
            self.message_post(body=_("No matching items found to deduct inventory."))
            self.fulfillment_inventory_deducted = True
            return

        # Validate the picking
        picking.action_confirm()
        picking.action_assign()
        
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
            
        picking.button_validate()

        self.fulfillment_inventory_deducted = True
        self.message_post(body=_("Inventory successfully deducted (Delivery: %s)") % picking.name)

    def write(self, vals):
        res = super().write(vals)
        # Check if task is being marked as done
        # State logic depends on Odoo version, but universally 'state' field or 'stage_id'
        # In Odoo 16/17 Project Task:
        # state selection: [('01_in_progress', 'In Progress'), ('1_done', 'Done'), ('04_waiting_normal', 'Waiting'), ...]
        # OR simple stages.
        
        # Let's check for state='1_done' if it exists, or check 'state' generally
        if 'state' in vals and vals['state'] in ['1_done', 'done']:
             for task in self:
                 if task.is_fulfillment_task and not task.fulfillment_inventory_deducted:
                     try:
                         task.action_fulfillment_deduct_inventory()
                     except Exception as e:
                         # Don't block the write, but log it
                         task.message_post(body=_("Failed to auto-deduct inventory: %s") % str(e))
        
        return res
