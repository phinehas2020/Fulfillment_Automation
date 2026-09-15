# Free Standard Shipping Policy

Shopify checkout offers a dedicated `Free Standard Shipping` flat rate alongside
the paid carrier-calculated services. The former blanket automatic shipping
discount is inactive so paid Priority and Express choices retain their full
checkout price.

The Odoo fulfillment add-on applies this purchase policy:

- `Free Standard Shipping`: buy the absolute lowest-cost structurally valid
  Shippo rate, regardless of carrier.
- Paid expedited choices: buy the lowest-cost structurally valid Shippo rate
  that satisfies the requested delivery-speed class.
- `Priority Mail Express` and other one-business-day/overnight choices require
  a one-day rate; generic Priority/Express choices require no more than three
  estimated business days unless an exact structured mapping overrides them.
- If no safe rate satisfies the paid delivery promise, hold the order for
  manual review instead of silently buying a slower label.

Rate audits record the selected service token, selected amount, cheapest
eligible amount, and policy version. A successful code deployment is not proof
of a physical label purchase; confirm the next real free-standard order's audit
row before calling the operational loop provider-verified.
