# Implementation Plan: Order Fulfillment Enhancements ✅ COMPLETE

## Overview
This plan covers three enhancements to the Shopify Fulfillment automation system:

1. **Exclude UPS Ground Saver** - Filter out "UPS Ground Saver" from shipping rate selection
2. **Use Customer Name for Task Titles** - Change To-Do task titles from "Pack Order #1234" to the customer's name
3. **Create Odoo Customers from Orders** - Automatically create/update `res.partner` records to build a customer database

---

## 1. Exclude "UPS Ground Saver" from Rate Selection

### Current Behavior
- In `shopify_order.py`, lines 440-472, rates are fetched via Shippo and the cheapest rate is selected
- There is no filtering of specific services

### Files to Modify
- `/shopify_fulfillment/models/shopify_order.py`

### Changes Required
After fetchingç rates from Shippo (line 441), add a filter to exclude "UPS Ground Saver":

```python
# After: rates = shippo.get_rates(self, box, self.env.company)
# Add filtering logic:
EXCLUDED_SERVICES = ["UPS Ground Saver"]
rates = [r for r in rates if r.get("servicelevel", {}).get("name") not in EXCLUDED_SERVICES]
```

### Location
- Around **line 441-447** in `shopify_order.py`

---

## 2. Use Customer Name for To-Do Task Titles

### Current Behavior
- In `print_agent.py`, line 146, task name is set to:
  ```python
  name = f"Pack Order {job.order_id.order_name or job.order_id.order_number}"
  ```
  This produces titles like "Pack Order #1234"

### Desired Behavior
- Use the customer's name instead, e.g., "John Smith"

### Files to Modify
- `/shopify_fulfillment/controllers/print_agent.py`

### Changes Required
Change line 146 to use customer name:

```python
# Replace:
# name = f"Pack Order {job.order_id.order_name or job.order_id.order_number}"
# With:
name = job.order_id.customer_name or f"Pack Order {job.order_id.order_name or job.order_id.order_number}"
```

This preserves the original format as a fallback if customer_name is empty.

### Location
- **Line 146** in `print_agent.py`

---

## 3. Create Odoo Customers from Orders

### Current Behavior
- Orders are imported from Shopify with customer info stored directly on `shopify.order`
- No `res.partner` (Odoo customer) records are created
- Customer data is not being collected for a reusable database

### Desired Behavior
- When an order is imported or processed, create/update a `res.partner` record
- Link the partner to the Shopify customer ID to avoid duplicates
- Store: name, email, phone, full shipping address

### Files to Modify
1. `/shopify_fulfillment/models/shopify_order.py` - Add customer creation logic
2. `/shopify_fulfillment/models/__init__.py` - (if new model needed, but we'll use existing res.partner)

### Implementation Approach

#### Option A: Extend `res.partner` with a `shopify_customer_id` field (Recommended)
This allows linking to the Shopify customer and prevents duplicate entries.

**New file: `/shopify_fulfillment/models/res_partner.py`**
```python
from odoo import fields, models

class ResPartner(models.Model):
    _inherit = "res.partner"
    
    shopify_customer_id = fields.Char(string="Shopify Customer ID", index=True)
```

**Update: `/shopify_fulfillment/models/__init__.py`**
Add import for the new model.

**Update: `shopify_order.py`** - Add method `_create_or_update_partner()`:
```python
def _create_or_update_partner(self):
    """Create or update res.partner from Shopify order data."""
    self.ensure_one()
    
    # Extract customer ID from raw payload if available
    payload = {}
    if self.raw_payload:
        try:
            payload = __import__('json').loads(self.raw_payload)
        except:
            pass
    
    customer_data = payload.get("customer", {})
    shopify_customer_id = str(customer_data.get("id", "")) if customer_data else ""
    
    Partner = self.env["res.partner"].sudo()
    partner = None
    
    # Try to find existing partner by Shopify customer ID
    if shopify_customer_id:
        partner = Partner.search([("shopify_customer_id", "=", shopify_customer_id)], limit=1)
    
    # Fallback: find by email
    if not partner and self.email:
        partner = Partner.search([("email", "=ilike", self.email)], limit=1)
    
    vals = {
        "name": self.customer_name or "Unknown Customer",
        "email": self.email,
        "phone": self.shipping_phone,
        "street": self.shipping_address_line1,
        "street2": self.shipping_address_line2,
        "city": self.shipping_city,
        "zip": self.shipping_zip,
        "customer_rank": 1,  # Mark as customer
    }
    
    # Set state and country if available
    if self.shipping_state:
        state = self.env["res.country.state"].search([
            ("code", "=", self.shipping_state),
            ("country_id.code", "=", self.shipping_country or "US")
        ], limit=1)
        if state:
            vals["state_id"] = state.id
    
    if self.shipping_country:
        country = self.env["res.country"].search([("code", "=", self.shipping_country)], limit=1)
        if country:
            vals["country_id"] = country.id
    
    if shopify_customer_id:
        vals["shopify_customer_id"] = shopify_customer_id
    
    if partner:
        # Update existing (only update fields that are not empty)
        update_vals = {k: v for k, v in vals.items() if v}
        partner.write(update_vals)
    else:
        # Create new
        partner = Partner.create(vals)
    
    return partner
```

#### Call Location
Add call to `_create_or_update_partner()` in either:
- `_prepare_order_vals_from_shopify()` after order creation
- `action_import_from_shopify()` after each order is created (around line 257)
- Or better: Override `create()` to call it automatically

Recommended: Call it right after order creation in `action_import_from_shopify()`:
```python
# After: order = self.create(order_vals)
order._create_or_update_partner()
```

---

## Implementation Order

1. **Create `res_partner.py`** - Add `shopify_customer_id` field
2. **Update `models/__init__.py`** - Import the new model
3. **Update `shopify_order.py`** - Add `_create_or_update_partner()` method and call it
4. **Update `shopify_order.py`** - Add rate filtering for UPS Ground Saver
5. **Update `print_agent.py`** - Change task name to use customer name

---

## Testing Checklist

- [ ] Import an order → Verify `res.partner` is created with correct data
- [ ] Import same customer again → Verify existing partner is updated, not duplicated
- [ ] Process order → Verify "UPS Ground Saver" is not selected even if cheapest
- [ ] Complete print job → Verify To-Do task title is customer name

---

## Upgrade Notes

After deployment, run:
```
odoo-bin -c /etc/odoo/odoo.conf -d your_db -u shopify_fulfillment --stop-after-init
```

This upgrades the module to install the new `shopify_customer_id` field on `res.partner`.
