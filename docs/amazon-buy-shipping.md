# Amazon Buy Shipping integration

The `shopify_fulfillment` addon routes Amazon marketplace orders through Amazon
Shipping API v2 when `amazon_shipping.enabled` is true and all LWA credentials
are configured. The feature is safe-off by default; ordinary orders and Amazon
orders continue to use Shippo until the Amazon path is explicitly enabled.

## Required Amazon setup

1. Complete Solution Provider Portal onboarding as a private solution provider.
2. Request and receive approval for **Direct-to-Consumer Shipping (Restricted)**
   and **Product Listing**. The former authorizes Amazon-order rating and
   cancellation; Amazon's current role mapping permits `purchaseShipment`
   through Product Listing (or Amazon Logistics).
3. Create a private SP-API application with both roles.
4. View its LWA client ID and client secret.
5. Self-authorize the application for the Homestead Gristmill US seller account
   and save the resulting refresh token.

Amazon requires an LWA client ID, LWA client secret, and self-authorization
refresh token. AWS IAM credentials and Signature V4 are not used.

## Runtime configuration

Prefer process environment variables on the Odoo Mac Mini:

- `AMAZON_LWA_CLIENT_ID`
- `AMAZON_LWA_CLIENT_SECRET`
- `AMAZON_LWA_REFRESH_TOKEN`
- `AMAZON_SP_API_ENDPOINT=https://sellingpartnerapi-na.amazon.com`
- `AMAZON_SHIPPING_BUSINESS_ID=AmazonShipping_US`
- `AMAZON_MARKETPLACE_ID=ATVPDKIKX0DER`
- `AMAZON_REQUIRED_VAS_PREFERENCES={}`
- `AMAZON_SHIPPING_ENABLED=false`

Equivalent admin-only Odoo parameters use the `amazon_shipping.*` keys described
in `data/config_params.xml`. Never add Amazon credentials to the general-user
fulfillment configuration wizard, source control, logs, screenshots, or issue
trackers.

## Safe activation sequence

1. Deploy and upgrade module version 0.5 with Amazon disabled.
2. Confirm Odoo, PostgreSQL, and the external tunnel are healthy.
3. Configure credentials without enabling purchases.
4. Exchange the refresh token for an access token.
5. Fetch OrderItems and Buy Shipping rates for one real unshipped Amazon order.
   Do not call `purchaseShipment` during this proof step.
6. Verify the API returns eligible rates, delivery promises, stable service IDs,
   and a supported 4x6 ZPL document specification.
   If an offer has a required value-added-service group with multiple choices,
   configure the desired service ID by group ID before enabling purchase.
7. Enable `amazon_shipping.enabled` during a controlled window.
8. Observe the first real order through rating, purchase-intent persistence,
   label printing, tracking push, and the rate audit.

## Selection rules

- Amazon rates are already classified as eligible/ineligible for the Amazon
  order. The addon requires a ZPL-capable rate, applies the order deadline, and
  selects the lowest adjusted total charge.
- Shippo fallback uses `servicelevel.token`, `carrier_account`, `estimated_days`,
  and the marketplace deadline. Carrier/service display names never drive the
  selection.
- An expired ship-by deadline, missing delivery evidence, currency mismatch,
  missing structured IDs, or an excessive premium creates a Manual Review hold
  before any label purchase.
- A purchase timeout is marked **Purchase Uncertain** and must be reconciled;
  the system does not blindly retry.
- If Amazon returned a shipment ID, the administrator can use **Reconcile
  Amazon Label Purchase** to retrieve the existing document and tracking number
  without buying another label.
- Reset/reprocess cancels Amazon labels through Amazon and Shippo labels through
  Shippo before deleting local records.
