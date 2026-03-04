// api_call.rl
// Demonstrates APICallTool: generic HTTP REST request.
// Trigger phrase: "call api <description>" / "fetch url <url>"

define Endpoint as "A public REST API endpoint to query".
define Request as "The HTTP request configuration sent to the endpoint".
define Response as "The structured data returned by the API".

Endpoint has url of "https://api.github.com/repos/python/cpython".
Endpoint has description of "GitHub repository metadata for CPython".

Request has method of "GET".
Request has header_accept of "application/vnd.github+json".
Request has timeout of 15.

Response has format of "json".

relate Request and Endpoint as "targets".
relate Endpoint and Response as "returns".

ensure call api to fetch url from Endpoint.
ensure determine Response star_count.
ensure determine Response open_issues.
