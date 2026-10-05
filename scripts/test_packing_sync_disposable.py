"""Run with Odoo shell ONLY against a fresh phone_packing_test_* database.

This commits synthetic fixtures to exercise genuinely independent cursors and
submission guards. Every Shopify interaction is replaced by an in-process fake;
no label purchase, print, inventory or accounting operation is invoked.
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import api, fields, SUPERUSER_ID


def run_packing_sync_disposable(env):
    if not env.cr.dbname.startswith("phone_packing_test_"):
        raise RuntimeError("Refusing sync integration fixtures outside a disposable phone_packing_test_* database.")
    company = env.company
    warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
    if not warehouse:
        raise RuntimeError("The disposable fixture database needs a stock warehouse.")

    def create_fixture():
        order = env["shopify.order"].sudo().create({"shopify_id": "SYNC-FIXTURE-%s" % uuid.uuid4(), "order_name": "#SYNC-FIXTURE", "company_id": company.id, "packing_warehouse_id": warehouse.id, "completion_policy_version": "packing_v1", "state": "ready_to_ship", "line_ids": [(0, 0, {"shopify_line_id": "991", "sku": "SYNC-FIXTURE", "title": "Synthetic sync fixture", "quantity": 1})]})
        shipment = env["fulfillment.shipment"].sudo().create({"order_id": order.id, "tracking_number": "1ZFIXTURE%s" % uuid.uuid4().hex.upper(), "carrier": "UPS", "line_ids": [(6, 0, order.line_ids.ids)], "line_quantities": json.dumps({str(order.line_ids.id): 1})})
        order.sudo().write({"shipment_id": shipment.id, "packing_operational_state": "complete"})
        shipment.sudo().write({"packing_state": "packed", "packing_sync_state": "pending"})
        env.cr.commit()
        return order.id, order.shopify_id, shipment.id

    class FakeShopify:
        def __init__(self):
            self.external = {}
            self.posts = 0
            self.accept_before_timeout = True
            self.observed_committed_guard = 0

        def get_order_fulfillments(self, shopify_id):
            return self.external.get(shopify_id, [])

        def create_fulfillment(self, order, tracking_info, line_items=None):
            # A genuinely separate DB cursor must already see the guard before
            # this fake POST executes. This proves its commit ordering.
            with env.registry.cursor() as cursor:
                fresh = api.Environment(cursor, SUPERUSER_ID, {})
                row = fresh["fulfillment.shipment"].search([("order_id", "=", order.id)], limit=1)
                assert row.packing_sync_state == "submitting", "POST preceded committed submission guard"
                assert row.packing_sync_attempts == 1
                assert row.packing_sync_attempt_token
                self.observed_committed_guard += 1
            assert line_items == [{"shopify_line_id": "991", "quantity": 1}], "Shipment must send exact explicit quantities"
            self.posts += 1
            if self.accept_before_timeout:
                self.external[order.shopify_id] = [{"id": 99000 + self.posts, "status": "success", "tracking_number": tracking_info["tracking_number"], "line_items": [{"id": 991, "quantity": 1}]}]
            raise TimeoutError("Synthetic Shopify response timeout")

    adapter = FakeShopify()
    model = env["fulfillment.shipment"].sudo()
    def fresh_state(shipment_id):
        with env.registry.cursor() as cursor:
            fresh = api.Environment(cursor, SUPERUSER_ID, {})
            return fresh["fulfillment.shipment"].browse(shipment_id).packing_sync_state
    def expire_lease(shipment_id):
        with env.registry.cursor() as cursor:
            fresh = api.Environment(cursor, SUPERUSER_ID, {})
            fresh["fulfillment.shipment"].browse(shipment_id).write({"packing_sync_started_at": fields.Datetime.now() - timedelta(seconds=180)})
            cursor.commit()

    with patch.object(type(env["shopify.order"]), "_get_shopify_api", autospec=True, return_value=adapter):
        accepted_order, accepted_shopify_id, accepted_shipment = create_fixture()
        model._packing_sync_one(accepted_shipment)
        assert fresh_state(accepted_shipment) == "submitting"
        assert adapter.posts == 1
        model._packing_sync_one(accepted_shipment)
        assert adapter.posts == 1, "Concurrent/recent submission was repeated"
        expire_lease(accepted_shipment)
        model._packing_sync_one(accepted_shipment)
        assert fresh_state(accepted_shipment) == "synced"
        assert adapter.posts == 1, "An accepted timeout caused a duplicate fulfillment"

        adapter.accept_before_timeout = False
        uncertain_order, uncertain_shopify_id, uncertain_shipment = create_fixture()
        model._packing_sync_one(uncertain_shipment)
        assert fresh_state(uncertain_shipment) == "submitting"
        assert adapter.posts == 2
        expire_lease(uncertain_shipment)
        model._packing_sync_one(uncertain_shipment)
        assert fresh_state(uncertain_shipment) == "review"
        assert adapter.posts == 2, "An ambiguous missing result was blindly reposted"
        model._packing_sync_one(uncertain_shipment)
        assert adapter.posts == 2
    return {"disposable_database": env.cr.dbname, "synthetic_posts": adapter.posts, "committed_guards_observed": adapter.observed_committed_guard, "accepted_timeout_reconciled": True, "ambiguous_timeout_held_without_repost": True, "recent_submission_lease_preserved": True, "exact_box_quantities": True, "no_real_provider_calls": True}


if "env" in globals():
    print(json.dumps(run_packing_sync_disposable(env), sort_keys=True))
