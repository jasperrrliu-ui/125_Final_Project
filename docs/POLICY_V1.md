# Retail policy 1.0.0 (draft_for_review)

Generated from `processed_data/policy/policy_v1.json` by `scripts/render_policy_doc.py`. Edit the JSON, not this file.

Evaluation clock (`as_of`): **2020-04-01**. Fixed evaluation clock. Synthetic purchase dates run from 2019-07-06 to 2020-03-30, so this date gives each return window (30 days, 90 days, 6 months) both eligible and ineligible orders.

Provenance tags: `abcd`: Transcribed from raw_data/guidelines.json; the path is in the rule's provenance field.; `tau_bench`: General rule modeled on the public tau-bench retail policy (Sierra, 2024).; `assumption`: Business assumption added for this project; needs review.

## General rules

- **G1_identity**: Before looking up any order or coupon, verify the customer with the email on the account, or with full name plus a zip code used on one of their orders. An account ID alone is not enough. _(tau_bench, assumption)_
- **G2_one_customer**: Serve one verified customer per conversation. A customer may raise several requests; handle them one at a time. _(tau_bench)_
- **G3_confirmation**: Before any action that changes an order, a payment, or an account, describe the exact action and get an explicit yes from the customer. _(tau_bench)_
- **G4_decline_vs_handoff**: If this policy says a request cannot be granted, explain why and close the request. Transfer to a human agent only when the request is outside the supported request types, a required fact cannot be obtained, or the systems disagree. _(tau_bench, abcd)_
- **G5_no_invention**: Never invent an order ID, coupon code, amount, date, or eligibility fact. System records take priority over what the customer says. _(tau_bench, assumption)_

## Identity

Accepted methods: `email` OR `full_name` + `zip_code`.

## Facts

| Fact | Sources, in priority order | Question to customer |
| --- | --- | --- |
| `member_level` | system:customer |  |
| `shipping_status` | system:order |  |
| `purchase_date` | system:order, customer | When did you buy this item? A date is fine, for example 2020-02-15. |
| `original_packaging` | system:order, customer | Is the item still in its original packaging? |
| `has_receipt` | customer | Do you still have the receipt for this purchase? |
| `oracle_customer_right` | system:coupon_check |  |

## Constants

- `shipped_statuses`: ["in transit", "out for delivery", "delivered"]
- `members`: ["bronze", "silver", "gold"]
- `upgrade_fee_cents`: 2000
- `standard_shipping_fee_cents`: 800
- `promo_discount_percent`: 20

## Rules by request family

Rules are checked in order. The first rule that is true decides. If a rule cannot be decided yet, the agent gets the missing fact (system first, then the customer) and checks again. If a needed fact cannot be obtained, the request is handed off unless the rule says `on_unavailable: skip`.

### return

Intents: `return_size`, `return_color`, `return_stain`. Required targets: `order_id`, `item`.

1. **return_gold**
   - When: `member_level` = "gold"
   - Then: Offer, then require an explicit yes: `return_by_mail` → `initiate_return` {"item_index": "$item.item_index", "return_method": "by_mail", "address_confirmed": true}; `return_in_store` → `initiate_return` {"item_index": "$item.item_index", "return_method": "in_store", "address_confirmed": true}; `return_drop_off` → `initiate_return` {"item_index": "$item.item_index", "return_method": "drop_off_center", "address_confirmed": true}
   - Note: Gold members can return any item.
   - Provenance: abcd:Product Defect/subflows/Return Due to Size/actions/2/subtext/0
2. **return_silver**
   - When: `member_level` = "silver" AND (`purchase_date` within 6 months of as_of OR `original_packaging` = true OR `has_receipt` = true)
   - Then: Offer, then require an explicit yes: `return_by_mail` → `initiate_return` {"item_index": "$item.item_index", "return_method": "by_mail", "address_confirmed": true}; `return_in_store` → `initiate_return` {"item_index": "$item.item_index", "return_method": "in_store", "address_confirmed": true}; `return_drop_off` → `initiate_return` {"item_index": "$item.item_index", "return_method": "drop_off_center", "address_confirmed": true}
   - Note: Silver members can return within 6 months of purchase, or with a receipt, or in original packaging.
   - Provenance: abcd:Product Defect/subflows/Return Due to Size/actions/2/subtext/1
3. **return_bronze**
   - When: `member_level` = "bronze" AND (`purchase_date` within 90 days of as_of OR `original_packaging` = true OR `has_receipt` = true)
   - Then: Offer, then require an explicit yes: `return_by_mail` → `initiate_return` {"item_index": "$item.item_index", "return_method": "by_mail", "address_confirmed": true}; `return_in_store` → `initiate_return` {"item_index": "$item.item_index", "return_method": "in_store", "address_confirmed": true}; `return_drop_off` → `initiate_return` {"item_index": "$item.item_index", "return_method": "drop_off_center", "address_confirmed": true}
   - Note: Bronze members can return within 90 days of purchase, or with a receipt, or in original packaging.
   - Provenance: abcd:Product Defect/subflows/Return Due to Size/actions/2/subtext/2
4. **return_guest**
   - When: `member_level` = "guest" AND (`purchase_date` within 30 days of as_of OR `has_receipt` = true)
   - Then: Offer, then require an explicit yes: `return_by_mail` → `initiate_return` {"item_index": "$item.item_index", "return_method": "by_mail", "address_confirmed": true}; `return_in_store` → `initiate_return` {"item_index": "$item.item_index", "return_method": "in_store", "address_confirmed": true}; `return_drop_off` → `initiate_return` {"item_index": "$item.item_index", "return_method": "drop_off_center", "address_confirmed": true}
   - Note: Guests can return within 30 days of purchase, or with a receipt.
   - Provenance: abcd:Product Defect/subflows/Return Due to Size/actions/2/subtext/3
5. **return_not_eligible**
   - When: `member_level` in ['gold', 'silver', 'bronze', 'guest']
   - Then: Decline (`return_window_or_proof_missing`): "I'm sorry, this item can't be returned under our policy for {member_level} members: the purchase is outside the return window, and there is no receipt or other qualifying proof."
   - Note: If none of the conditions above hold, apologize, explain, and close the request.
   - Provenance: abcd:Product Defect/subflows/Return Due to Size/actions/3/subtext/1

### cancel

Intents: `manage_cancel`. Required targets: `order_id`, `item`.

1. **cancel_order_received**
   - When: `shipping_status` = "order received"
   - Then: Offer, then require an explicit yes: `cancel_and_refund` → `cancel_item_and_refund` {"item_index": "$item.item_index", "amount_cents": "$item.amount_cents"}
   - Note: If the order has not shipped, remove the unwanted item and refund it now.
   - Provenance: abcd:Order Issue/subflows/Manage Cancel/actions/2/subtext/1
2. **cancel_shipped_member**
   - When: `shipping_status` in shipped_statuses ['in transit', 'out for delivery', 'delivered'] AND `member_level` in members ['bronze', 'silver', 'gold']
   - Then: Offer, then require an explicit yes: `refund_now` → `refund_item` {"item_index": "$item.item_index", "amount_cents": "$item.amount_cents"}
   - Note: If shipped and the customer is a Bronze, Silver, or Gold member, refund now; the customer returns the item after arrival. v1 includes Bronze, as the source text does; the source's unclear form instruction is about data entry, not eligibility.
   - Provenance: abcd:Order Issue/subflows/Manage Cancel/actions/3/subtext/0, assumption
3. **cancel_shipped_guest**
   - When: `shipping_status` in shipped_statuses ['in transit', 'out for delivery', 'delivered'] AND `member_level` = "guest"
   - Then: Decline (`guest_wait_for_arrival`): "Your order has already shipped, so I can't cancel the {item_name} now. Once it arrives, contact us and we'll start a return."
   - Note: Guests with a shipped order must wait for arrival, then start a return.
   - Provenance: abcd:Order Issue/subflows/Manage Cancel/actions/3/subtext/1

### upgrade

Intents: `manage_upgrade`. Required targets: `order_id`.

1. **upgrade_already_shipped**
   - When: `shipping_status` in shipped_statuses ['in transit', 'out for delivery', 'delivered']
   - Then: Decline (`already_shipped`): "Your order has already shipped, so the delivery date can't be changed. It will most likely arrive today or tomorrow."
   - Provenance: abcd:Order Issue/subflows/Manage Upgrade/actions/2/subtext/2
2. **upgrade_gold**
   - When: `shipping_status` = "order received" AND `member_level` = "gold"
   - Then: Offer, then require an explicit yes: `upgrade_free` → `reschedule_delivery` {"delivery_window": "tomorrow"}
   - Provenance: abcd:Order Issue/subflows/Manage Upgrade/actions/3/subtext/1
3. **upgrade_paid**
   - When: `shipping_status` = "order received" AND `member_level` in ['guest', 'bronze', 'silver']
   - Then: Offer, then require an explicit yes: `upgrade_with_fee` → `reschedule_delivery` {"delivery_window": "tomorrow", "charge_amount_cents": "$const.upgrade_fee_cents"}
   - Note: Non-Gold customers pay $20. The customer's yes to this option is the charge consent; a no closes the request with no change.
   - Provenance: abcd:Order Issue/subflows/Manage Upgrade/actions/3/subtext/2, assumption

### downgrade

Intents: `manage_downgrade`. Required targets: `order_id`.

1. **downgrade_order_received**
   - When: `shipping_status` = "order received"
   - Then: Offer, then require an explicit yes: `delay_to_next_week` → `reschedule_delivery` {"delivery_window": "next_week"}
   - Provenance: abcd:Order Issue/subflows/Manage Downgrade/actions/3/subtext/0
2. **downgrade_shipped_member**
   - When: `shipping_status` in shipped_statuses ['in transit', 'out for delivery', 'delivered'] AND `member_level` in members ['bronze', 'silver', 'gold']
   - Then: Offer, then require an explicit yes: `shipping_fee_credit` → `credit_shipping_fee` {"amount_cents": "$const.standard_shipping_fee_cents"}
   - Note: The source gives no fee amount for this flow; v1 uses the $8 standard shipping fee from the ABCD Shipping Cost flow.
   - Provenance: abcd:Order Issue/subflows/Manage Downgrade/actions/3/subtext/0, abcd:Shipping Issue/subflows/Shipping Cost, assumption
3. **downgrade_shipped_guest**
   - When: `shipping_status` in shipped_statuses ['in transit', 'out for delivery', 'delivered'] AND `member_level` = "guest"
   - Then: Decline (`shipped_guest_no_change`): "I'm sorry, the order has already shipped and there's nothing I can change for it."
   - Provenance: abcd:Order Issue/subflows/Manage Downgrade/actions/3/subtext/1

### promo

Intents: `promo_code_invalid`, `promo_code_out_of_date`. Required targets: `coupon_code`.

1. **promo_platform_error**
   - When: `oracle_customer_right` = true
   - If a fact is unavailable: skip
   - Then: Offer, then require an explicit yes: `issue_code_platform_error` → `issue_promo_code` {"discount_percent": "$const.promo_discount_percent"}
   - Note: If the Oracle says the customer is right, issue a new code. If the Oracle can't answer, skip this rule.
   - Provenance: abcd:Purchase Dispute/subflows/Promo Code Invalid/actions/2/subtext/3
2. **promo_member_goodwill**
   - When: `member_level` in members ['bronze', 'silver', 'gold']
   - Then: Offer, then require an explicit yes: `issue_code_goodwill` → `issue_promo_code` {"discount_percent": "$const.promo_discount_percent"}
   - Note: Members get a new code even when the original code was not valid.
   - Provenance: abcd:Purchase Dispute/subflows/Promo Code Invalid/actions/3/subtext/2
3. **promo_guest_no_code**
   - When: `member_level` = "guest" AND `oracle_customer_right` = false
   - Then: Decline (`guest_code_not_valid`): "I checked our records: the code itself couldn't be applied, and I'm sorry that I'm not able to issue a replacement for guest accounts."
   - Note: If a guest's code really was not valid, apologize. If the Oracle can't answer for a guest, hand off.
   - Provenance: abcd:Purchase Dispute/subflows/Promo Code Invalid/actions/3/subtext/3
