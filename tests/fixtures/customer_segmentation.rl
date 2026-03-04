// customer_segmentation.rl
// Classify a customer into HighValue or Standard based on purchases + tenure.

define Customer as "A person who purchases products from the store".
define HighValue as "Customer segment requiring premium support and offers".
define Standard as "Customer segment with regular support".

Customer has total_purchases of 15000.
Customer has account_age_days of 400.
Customer has support_tickets of 2.

if Customer has total_purchases > 10000 and account_age_days > 365,
    then ensure Customer is HighValue.

ensure determine Customer segment.
ensure recommend Customer support tier.
