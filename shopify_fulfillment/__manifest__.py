{
    "name": "Shopify Fulfillment Automation",
    "summary": "Automated fulfillment via Shopify Shipping API with print queue dispatch",
    "version": "0.1.2",
    "license": "LGPL-3",
    "author": "Your Company",
    "website": "",
    "depends": ["base"],
    "application": True,
    "data": [
        "security/ir.model.access.csv",
        "data/default_boxes.xml",
        "views/shopify_order_views.xml",
        "views/fulfillment_box_views.xml",
        "views/print_job_views.xml",
        "views/print_test_wizard_views.xml",
        "views/menu.xml",
    ],
    "description": """
    Skeleton for Shopify fulfillment automation.
    Implements webhook intake, rate shopping, label purchasing, and print queue per spec.
    """,
}
