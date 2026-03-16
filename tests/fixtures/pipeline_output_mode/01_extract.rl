// 01_extract.rl
// Stage 1 — Data Extraction  (output_mode: rl)
//
// The LLM must respond in plain RelateLang attribute statements ONLY.
// No prose, no explanations, no markdown — just RelateLang lines.
//
// Every response must contain lines in one of these exact forms:
//   EntityName has attributeName of "string value".
//   EntityName has attributeName of 123.
//   EntityName is predicateLabel.
//
// Full RLParser attempts a complete parse first; a regex fallback
// extracts individual attribute/predicate lines from mixed prose.
// RetryManager re-prompts with an RL hint if no RL is returned.

define Customer as "A person purchasing a product from the online store".
define Product  as "An item listed in the product catalogue".

// Seed facts — already known. Confirm them by repeating them plus any new facts.
Customer has name             of "Alice Müller".
Customer has email            of "alice@example.com".
Customer has country          of "DE".
Customer has account_age_days of 412.

Product has sku         of "WIDGET-42".
Product has category    of "electronics".
Product has unit_price  of 149.99.
Product has stock_level of 23.

relate Customer and Product as "viewed".

// ── Goals ─────────────────────────────────────────────────────────────────────
// For each goal below, respond ONLY with RelateLang attribute statements.
// Do NOT write sentences, headings, or explanations.
// Every line must follow exactly: EntityName has attrName of value.
// Example correct response:
//   Customer has name of "Alice Müller".
//   Customer has email of "alice@example.com".

// Goal 1: Confirm Customer contact details are present and set
//   purchase_eligible to "yes" or "no".
//   Required output lines (include ALL of them):
//     Customer has name of "...".
//     Customer has email of "...".
//     Customer has country of "...".
//     Customer has account_age_days of NNN.
//     Customer has purchase_eligible of "yes".
ensure Customer has name and email confirmed and purchase_eligible attribute set to yes or no.

// Goal 2: Confirm Product catalogue entry and set availability to in_stock or out_of_stock.
//   Required output lines (include ALL of them):
//     Product has sku of "...".
//     Product has category of "...".
//     Product has unit_price of NNN.
//     Product has stock_level of NNN.
//     Product has availability of "in_stock".
ensure Product has sku and stock_level confirmed and availability attribute set to in_stock or out_of_stock.

// Goal 3: Set Customer purchase_eligible based on account_age_days.
//   Rule: if account_age_days >= 365 then purchase_eligible is "yes" else "no".
//   Required output lines (include ALL of them — copy seed facts plus the new one):
//     Customer has name of "...".
//     Customer has email of "...".
//     Customer has country of "...".
//     Customer has account_age_days of NNN.
//     Customer has purchase_eligible of "yes".
ensure Customer has purchase_eligible set by evaluating whether account_age_days is at least 365.
