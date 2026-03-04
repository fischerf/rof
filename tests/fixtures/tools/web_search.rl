// web_search.rl
// Demonstrates WebSearchTool: live web search via DuckDuckGo / SerpAPI / Brave.
// Trigger phrase: "retrieve web_information about <topic>"

define Topic as "Subject of the web search query".
define SearchConfig as "Parameters controlling the web search behaviour".
define SearchSummary as "Aggregated findings from retrieved web results".

Topic has subject of "Python 3.13 new features".
Topic has relevance of "programming language release notes".

SearchConfig has max_results of 5.
SearchConfig has backend of "auto".

relate Topic and SearchConfig as "searched with".
relate SearchSummary and Topic as "summarises findings for".

ensure retrieve web_information about Topic subject.
ensure determine SearchSummary key_highlights.
