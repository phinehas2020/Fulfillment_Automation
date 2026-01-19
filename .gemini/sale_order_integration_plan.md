# Implementation Plan: Shopify → Odoo Sale Order Integration

## Overview
Create Odoo `sale.order` records when Shopify orders are fulfilled. This provides:
- **Revenue tracking** in Odoo reports
- **Items sold** history per product
- **Customer purchase history** (linked to `res.partner`)
- **Proper sales analytics** and dashboards

---

## Current State
- Shopify orders exist in custom `shopify.order` model
- Inventory is deducted via `stock.picking` (delivery orders) when fulfillment tasks complete
- Customers are created/updated in `res.partner` with `shopify_customer_id`
- **No sale.order records are created**

---

## Proposed Flow

```
Shopify Order → shopify.order (import)
                    ↓
           [Order Processed / Printed]
                    ↓
           project.task marked "Done"
                    ↓
           ┌───────────────────────────┐
           │ 1. Create stock.picking   │  ← Already implemented
           │ 2. Deduct inventory       │  ← Already implemented
           │ 3. Create sale.order  NEW │  ← This plan
           └───────────────────────────┘
```

---

## Implementation Approach

### Option A: Create Sale Order at Fulfillment (Recommended)
**When**: When the fulfillment task is marked "Done" (same time as inventory deduction)
**Why**: 
- Sale is only recorded when the order is actually shipped
- Matches industry practice (revenue recognized at fulfillment)
- Avoids recording cancelled orders as sales

### Option B: Create Sale Order at Import
**When**: When the Shopify order is first imported
**Why**: 
- Shows pending sales in pipeline
- But would need cleanup if orders are cancelled

**Recommendation**: **Option A** - Cleaner and more accurate for accounting.

---

## Technical Implementation

### Files to Modify

| File | Change |
|------|--------|
| `models/shopify_order.py` | Add `sale_order_id` field |
| `models/project_task.py` | Add sale order creation in `action_fulfillment_deduct_inventory()` |
| `views/shopify_order_views.xml` | Display link to sale order |

---

### Step 1: Add Field to `shopify.order`

```python
# In shopify_order.py, add to the model fields:

sale_order_id = fields.Many2one(
    "sale.order", 
    string="Sale Order", 
    readonly=True,
    help="Linked Odoo Sale Order created upon fulfillment"
)
```

---

### Step 2: Create Sale Order Method

Add a new method to `shopify.order`:

```python
def _create_sale_order(self):
    """Create an Odoo sale.order from this Shopify order."""
    self.ensure_one()
    
    if self.sale_order_id:
        return self.sale_order_id  # Already exists
    
    # Find or create partner
    partner = self._create_or_update_partner()
    
    # Prepare sale order lines
    order_lines = []
    for line in self.line_ids:
        if not line.requires_shipping:
            continue
            
        sku = (line.sku or "").strip()
        product = None
        
        # Find product by SKU
        if sku:
            product = self.env["product.product"].search([
                ("default_code", "=ilike", sku)
            ], limit=1)
            
            if not product:
                product = self.env["product.product"].search([
                    ("product_tmpl_id.default_code", "=ilike", sku)
                ], limit=1)
        
        if not product:
            # Create a placeholder or skip
            _logger.warning("No product found for SKU %s in order %s", sku, self.order_name)
            continue
        
        # Get price from Shopify payload if available
        price_unit = 0.0
        if self.raw_payload:
            try:
                payload = json.loads(self.raw_payload)
                for li in payload.get("line_items", []):
                    if str(li.get("id")) == str(line.shopify_line_id):
                        price_unit = float(li.get("price", 0))
                        break
            except:
                pass
        
        order_lines.append((0, 0, {
            "product_id": product.id,
            "product_uom_qty": line.quantity,
            "price_unit": price_unit,
            "name": line.title or product.name,
        }))
    
    if not order_lines:
        _logger.warning("No valid lines for sale order creation: %s", self.order_name)
        return False
    
    # Create the sale order
    sale_vals = {
        "partner_id": partner.id,
        "origin": self.order_name or self.order_number,
        "client_order_ref": self.shopify_id,
        "order_line": order_lines,
        "state": "sale",  # Skip quotation, go straight to confirmed
    }
    
    # Add shipping line if we have shipment info
    if self.shipment_id and self.shipment_id.rate_amount:
        # Find or create a shipping product
        shipping_product = self.env.ref(
            "shopify_fulfillment.product_shipping_cost", 
            raise_if_not_found=False
        )
        if not shipping_product:
            shipping_product = self.env["product.product"].search([
                ("default_code", "=", "SHIPPING")
            ], limit=1)
        
        if shipping_product:
            order_lines.append((0, 0, {
                "product_id": shipping_product.id,
                "product_uom_qty": 1,
                "price_unit": self.shipment_id.rate_amount,
                "name": f"Shipping - {self.shipment_id.carrier} {self.shipment_id.service}",
            }))
    
    sale_order = self.env["sale.order"].create(sale_vals)
    self.sale_order_id = sale_order.id
    
    _logger.info("Created sale order %s for Shopify order %s", sale_order.name, self.order_name)
    return sale_order
```

---

### Step 3: Call from Fulfillment Task

In `project_task.py`, inside `action_fulfillment_deduct_inventory()`, add after successful picking validation:

```python
# After: self.fulfillment_inventory_deducted = True

# Create sale order
try:
    self.shopify_order_id._create_sale_order()
except Exception as e:
    _logger.warning("Failed to create sale order: %s", e)
    self.message_post(body=_("Sale order creation failed: %s") % str(e))
```

---

### Step 4: Update View

Add to `shopify_order_views.xml` in the form view (perhaps in Shipment tab):

```xml
<field name="sale_order_id" readonly="1"/>
```

---

### Step 5: (Optional) Create Shipping Product

Add to `data/` folder:

```xml
<!-- data/product_data.xml -->
<odoo>
    <record id="product_shipping_cost" model="product.product">
        <field name="name">Shipping Cost</field>
        <field name="default_code">SHIPPING</field>
        <field name="type">service</field>
        <field name="list_price">0</field>
        <field name="sale_ok">True</field>
        <field name="purchase_ok">False</field>
    </record>
</odoo>
```

---

## Considerations

### Price Handling
- **Option 1**: Pull price from Shopify payload (`line_items[].price`) ← Recommended
- **Option 2**: Use Odoo product's `list_price`
- **Option 3**: Store price on `shopify.order.line` model

### Tax Handling
- Shopify handles taxes separately
- Option: Import tax as a separate line or use Odoo's fiscal positions
- **Simple approach**: Don't include tax in Odoo SO (record net revenue only)

### Currency
- Shopify sends currency in payload
- Odoo sale orders have `pricelist_id` which includes currency
- If multi-currency, may need to set pricelist or use `currency_id` field

### Cancelled Orders
- If a Shopify order is cancelled after fulfillment, the sale order will remain
- Consider: Add `state` check or a cancel sync mechanism

---

## Testing Checklist

- [ ] Process an order → Sale order is created
- [ ] Check sale order has correct customer (res.partner)
- [ ] Check sale order lines match Shopify items
- [ ] Check prices pulled from Shopify correctly
- [ ] Verify SKU matching works (exact and case-insensitive)
- [ ] Process same order twice → Only one sale order created
- [ ] Check link visible in Shopify Order form

---

## Dependencies

- `sale` module must be installed
- Products must have matching `default_code` (Internal Reference) for SKU matching
- `res.partner` linkage (already implemented)

---

## Migration / Backfill

For existing fulfilled orders without sale.order:
1. Create a server action or script
2. Search for `shopify.order` where `state = 'shipped'` and `sale_order_id = False`
3. Call `_create_sale_order()` on each

```python
# Via Odoo shell:
orders = env['shopify.order'].search([('state', '=', 'shipped'), ('sale_order_id', '=', False)])
for order in orders:
    try:
        order._create_sale_order()
        env.cr.commit()
    except Exception as e:
        print(f"Failed {order.order_name}: {e}")
        env.cr.rollback()
```

---

## Estimated Effort

| Task | Time |
|------|------|
| Add field + method to shopify_order.py | 30 min |
| Update project_task.py trigger | 15 min |
| Update views | 10 min |
| Create shipping product data | 10 min |
| Testing | 30 min |
| **Total** | ~1.5 hours |
