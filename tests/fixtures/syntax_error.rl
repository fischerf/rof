// syntax_error.rl
// Missing period on the last statement → ParseError (E001)
// The tokenizer accumulates a buffer until it sees '.'.
// When EOF is reached with a non-empty buffer it raises ParseError.

define Product as "An item for sale".

Product has price of 99.99.
ensure check Product availability.

Product has stock_level of 42
// ↑ Missing trailing period — this is the last non-comment line,
//   so the tokenizer hits EOF with a non-empty buffer and raises E001.
