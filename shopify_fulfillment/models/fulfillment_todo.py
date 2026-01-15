from odoo import api, fields, models, _
from odoo.exceptions import UserError

class FulfillmentTodo(models.Model):
    _name = "fulfillment.todo"
    _description = "Fulfillment To-Do Task"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    order_id = fields.Many2one("shopify.order", string="Shopify Order", required=True, readonly=True)
    user_id = fields.Many2one("res.users", string="Assigned Employee", tracking=True)
    name = fields.Char(string="Task Name", compute="_compute_name", store=True)

    state = fields.Selection([
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("cancel", "Cancelled")
    ], default="pending", string="Status", tracking=True)
    
    line_ids = fields.One2many("fulfillment.todo.line", "todo_id", string="Items to Pack")
    
    date_completed = fields.Datetime(string="Completed At", readonly=True)

    @api.depends("order_id.order_name", "order_id.order_number")
    def _compute_name(self):
        for record in self:
            record.name = f"Pack {record.order_id.order_name or record.order_id.order_number or 'Order'}"

    def action_complete(self):
        """Mark as complete and decrement Odoo inventory."""
        self.ensure_one()
        if self.state == 'completed':
            return True

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
            'origin': self.order_id.order_name or self.order_id.order_number,
            'move_type': 'direct',
        }
        
        picking = self.env['stock.picking'].create(picking_vals)
        
        moves = []
        for line in self.line_ids:
            # Match product by SKU (internal_reference)
            product = self.env['product.product'].search([('default_code', '=', line.sku)], limit=1)
            if not product:
                # Fallback: maybe it's on the template?
                product = self.env['product.product'].search([('product_tmpl_id.default_code', '=', line.sku)], limit=1)
            
            if not product:
                # If we still can't find it, we might want to skip or error.
                # For now, let's log a message in the chatter and skip this item but record the failure
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
            raise UserError(_("No matching products found in Odoo for any of the items in this order. Please check your SKUs."))

        # Validate the picking
        picking.action_confirm()
        picking.action_assign()
        
        # We try to validate it immediately. If stock is missing, Odoo might create a backorder or 
        # we might just force it depending on how the user wants. 
        # Typically for "post-facto" inventory adjustment, we just want it done.
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
            
        picking.button_validate()

        self.write({
            'state': 'completed',
            'date_completed': fields.Datetime.now()
        })
        return True

    def action_cancel(self):
        self.write({'state': 'cancel'})


class FulfillmentTodoLine(models.Model):
    _name = "fulfillment.todo.line"
    _description = "Fulfillment To-Do line"

    todo_id = fields.Many2one("fulfillment.todo", ondelete="cascade")
    sku = fields.Char(string="SKU")
    title = fields.Char(string="Title")
    quantity = fields.Integer(string="Quantity")
