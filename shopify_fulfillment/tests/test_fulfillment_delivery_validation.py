from unittest.mock import patch

from odoo import exceptions
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFulfillmentDeliveryValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.task_model = self.env["project.task"]
        self.product = self.env["product.product"].create({
            "name": "Fulfillment validation test product",
            "is_storable": True,
        })
        self.source = self.env.ref("stock.stock_location_stock")
        self.destination = self.env.ref("stock.stock_location_customers")
        self.picking = self.env["stock.picking"].create({
            "picking_type_id": self.env.ref("stock.picking_type_out").id,
            "location_id": self.source.id,
            "location_dest_id": self.destination.id,
        })
        self.move = self.env["stock.move"].create({
            "name": self.product.name,
            "picking_id": self.picking.id,
            "product_id": self.product.id,
            "product_uom_qty": 4,
            "product_uom": self.product.uom_id.id,
            "location_id": self.source.id,
            "location_dest_id": self.destination.id,
        })
        self.env["stock.move.line"].create([
            {
                "move_id": self.move.id,
                "picking_id": self.picking.id,
                "product_id": self.product.id,
                "product_uom_id": self.product.uom_id.id,
                "location_id": self.source.id,
                "location_dest_id": self.destination.id,
                "quantity": 2,
            },
            {
                "move_id": self.move.id,
                "picking_id": self.picking.id,
                "product_id": self.product.id,
                "product_uom_id": self.product.uom_id.id,
                "location_id": self.source.id,
                "location_dest_id": self.destination.id,
                "quantity": 2,
            },
        ])

    def test_done_quantity_is_not_duplicated_across_move_lines(self):
        self.task_model._set_picking_done_quantities(self.picking)

        self.assertEqual(self.move.quantity, 4)
        self.assertEqual(sum(self.move.move_line_ids.mapped("quantity")), 4)
        self.assertTrue(self.move.picked)

    def test_sms_is_skipped_and_incomplete_validation_is_rejected(self):
        action = {
            "type": "ir.actions.act_window",
            "res_model": "confirm.stock.sms",
        }
        picking_model = type(self.picking)

        with patch.object(picking_model, "button_validate", autospec=True, return_value=action) as validate:
            with self.assertRaisesRegex(exceptions.UserError, "did not reach Done"):
                self.task_model._validate_fulfillment_picking(self.picking)

        validated_picking = validate.call_args.args[0]
        self.assertTrue(validated_picking.env.context.get("skip_sms"))

    def test_completed_validation_is_accepted(self):
        picking_model = type(self.picking)

        def complete(picking):
            picking.write({"state": "done"})
            return True

        with patch.object(picking_model, "button_validate", autospec=True, side_effect=complete):
            result = self.task_model._validate_fulfillment_picking(self.picking)

        self.assertTrue(result)
        self.assertEqual(self.picking.state, "done")
