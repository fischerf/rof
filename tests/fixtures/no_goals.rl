// no_goals.rl
// Valid syntax but no 'ensure' statements → W001

define Order as "A customer purchase transaction".
define Product as "An item available for sale".

Order has amount of 250.
Order has item_count of 3.
Product has name of "Wireless Headphones".

relate Order and Product as "contains".
