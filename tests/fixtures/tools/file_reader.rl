// file_reader.rl
// Demonstrates FileReaderTool: read and extract content from a file.
// Trigger phrase: "read file <path>" / "extract text from <path>"

define Document as "A text file whose content is to be ingested".
define ExtractedContent as "The text content parsed from the document".
define Summary as "A concise summary of the document contents".

Document has path of "README.md".
Document has format of "markdown".
Document has max_chars of 4000.

ExtractedContent has encoding of "utf-8".

relate Document and ExtractedContent as "yields".
relate ExtractedContent and Summary as "distilled into".

ensure read file from Document path.
ensure determine Summary key_points.
