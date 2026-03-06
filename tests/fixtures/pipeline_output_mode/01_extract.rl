// 01_extract.rl
// Stage 1 — Data Extraction  (output_mode: rl)
//
// The LLM is asked to respond in plain RelateLang text.
// Full RLParser attempts a complete parse first; a regex fallback
// extracts individual attribute/predicate lines from mixed prose.
// RetryManager re-prompts with an RL hint if no RL is returned.

define Customer as "A person purchasing a product from the online store".
define Product  as "An item listed in the product catalogue".

// Seed facts — the LLM will validate, normalise, and enrich these.
Customer has name             of "Alice Müller".
Customer has email            of "alice@example.com".
Customer has country          of "DE".
Customer has account_age_days of 412.

Product has sku         of "WIDGET-42".
Product has category    of "electronics".
Product has unit_price  of 149.99.
Product has stock_level of 23.

relate Customer and Product as "viewed".

// Goals — the LLM answers each in plain RelateLang attribute statements.
ensure validate Customer contact details and confirm email format is valid.
ensure validate Product catalogue entry and confirm stock availability.
ensure summarise Customer purchase eligibility based on account age.
