{
    "name": "Shopify Fulfillment Automation",
    "summary": "Automated fulfillment via Shopify Shipping API with print queue dispatch",
    "version": "0.2.5",
    "license": "LGPL-3",
    "author": "Your Company",
    "website": "",
    "depends": ["base", "base_setup", "stock", "mail", "project", "sale"],
    "installable": True,
    "application": True,
    "data": [
        "security/ir.model.access.csv",
        "data/config_params.xml",
        "data/default_boxes.xml",
        "data/product_data.xml",
        "views/shopify_order_views.xml",
        "views/fulfillment_box_views.xml",
        "views/print_job_views.xml",
        "views/print_test_wizard_views.xml",
        "views/shopify_config_wizard_views.xml",
        "views/project_task_views.xml",
        "views/recent_shipment_views.xml",
        "views/menu.xml",
    ],
    "description": """
    Skeleton for Shopify fulfillment automation.
    Implements webhook intake, rate shopping, label purchasing, and print queue per spec.
    """,
}
