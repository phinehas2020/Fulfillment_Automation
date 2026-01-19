import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

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

        _logger.info("Starting inventory deduction for task %s (Order: %s)", self.id, self.shopify_order_id.order_name)

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
        for line in self.shopify_order_id.line_ids:
            if not line.requires_shipping:
                _logger.info("Skipping line %s: No shipping required", line.title)
                continue

            sku = (line.sku or "").strip()
            if not sku:
                self.message_post(body=_("Skipping item '%s' - No SKU provided.") % line.title)
                continue

            # Match product by SKU (internal_reference)
            # 1. Exact match
            product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
            
            # 2. Case-insensitive match if not found
            if not product:
                product = self.env['product.product'].search([('default_code', '=ilike', sku)], limit=1)
            
            # 3. Fallback: Template exact match
            if not product:
                product = self.env['product.product'].search([('product_tmpl_id.default_code', '=', sku)], limit=1)
            
            # 4. Fallback: Template case-insensitive match
            if not product:
                product = self.env['product.product'].search([('product_tmpl_id.default_code', '=ilike', sku)], limit=1)
            
            if not product:
                msg = _("Could not find Odoo product for SKU: '%s' (Item: %s). Inventory was not decremented for this item.") % (sku, line.title)
                self.message_post(body=msg)
                _logger.warning("Order %s: %s", self.shopify_order_id.order_name, msg)
                continue

            _logger.info("Found product %s for SKU %s", product.display_name, sku)

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
            _logger.warning("No moves created for picking. Picking unlinked.")
            # We DON'T mark it as deducted so the user can fix and try again
            self.message_post(body=_("No matching items found in Odoo for any of the order lines. Inventory deduction skipped. Please check your product SKUs."))
            return

        # Validate the picking
        try:
            picking.action_confirm()
            picking.action_assign()
            
            # Handle both Odoo 16 and 17 field names if possible, but prioritize 17
            for move in picking.move_ids:
                if hasattr(move, 'quantity'): # Odoo 17+
                    move.quantity = move.product_uom_qty
                elif hasattr(move, 'quantity_done'): # Odoo 16
                    move.quantity_done = move.product_uom_qty
                
                if hasattr(move, 'picked'):
                    move.picked = True
                
            picking.button_validate()
            self.fulfillment_inventory_deducted = True
            self.message_post(body=_("Inventory successfully deducted (Delivery: %s)") % picking.name)
            _logger.info("Inventory successfully deducted for Order %s", self.shopify_order_id.order_name)
        except Exception as e:
            _logger.exception("Failed to validate picking for task %s", self.id)
            self.message_post(body=_("Failed to validate inventory delivery: %s") % str(e))
            # Keep the picking around so it can be fixed manually if needed
            # But don't mark as deducted

    @api.model_create_multi
    def create(self, vals_list):
        tasks = super().create(vals_list)
        for task in tasks:
            if task.is_fulfillment_task and task.state in ['1_done', 'done'] and not task.fulfillment_inventory_deducted:
                try:
                    task.action_fulfillment_deduct_inventory()
                except Exception as e:
                    _logger.exception("Error in auto-deduct inventory on create")
                    task.message_post(body=_("Background error during inventory deduction: %s") % str(e))
        return tasks

    def write(self, vals):
        res = super().write(vals)
        # Check if task is being marked as done
        if 'state' in vals and vals['state'] in ['1_done', 'done']:
             for task in self:
                  if task.is_fulfillment_task and not task.fulfillment_inventory_deducted:
                      try:
                          task.action_fulfillment_deduct_inventory()
                      except Exception as e:
                          # Don't block the write
                          _logger.exception("Error in auto-deduct inventory on write")
                          task.message_post(body=_("Background error during inventory deduction: %s") % str(e))
        
        return res

