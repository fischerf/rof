"""Shared provider error classes, HTTP error helper, and shared JSON schema constants."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ProviderError",
    "RateLimitError",
    "ContextLimitError",
    "AuthError",
    "_classify_http_error",
    "ROF_GRAPH_UPDATE_SCHEMA",
    "_ROF_TOOL_DEFINITION",
]


class ProviderError(Exception):
    """Raised when an LLM provider returns an error that cannot be retried."""

    def __init__(self, msg: str, status_code: int = 0, raw: Any = None):
        super().__init__(msg)
        self.status_code = status_code
        self.raw = raw


class RateLimitError(ProviderError):
    """Provider returned HTTP 429 or equivalent."""


class ContextLimitError(ProviderError):
    """Prompt exceeds the model's context window."""


class AuthError(ProviderError):
    """API key missing or invalid."""


def _classify_http_error(status_code: int, body: str) -> ProviderError:
    """Map HTTP status codes to typed ProviderErrors."""
    msg = f"HTTP {status_code}: {body[:200]}"
    if status_code == 429:
        return RateLimitError(msg, status_code)
    if status_code in (401, 403):
        return AuthError(msg, status_code)
    return ProviderError(msg, status_code)


# JSON Schema for structured LLM responses (used by all providers in JSON mode)
ROF_GRAPH_UPDATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "attributes": {
            "type": "array",
            "description": (
                "Structured entity attribute updates.  Each item sets one "
                "attribute on one entity.  Use for numeric values, short "
                "string values, booleans, and classification labels."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["entity", "name", "value"],
                "additionalProperties": False,
            },
        },
        "predicates": {
            "type": "array",
            "description": (
                "Categorical / boolean conclusions about an entity.  "
                "Each item asserts that an entity satisfies a label "
                "(e.g. 'high_value', 'approved').  Pick exactly ONE "
                "value per decision — never enumerate all options."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity", "value"],
                "additionalProperties": False,
            },
        },
        "prose": {
            "type": "string",
            "description": (
                "Free-form text output for goals that require a natural-language "
                "answer: analysis reports, recommendations, summaries, "
                "explanations, or any other multi-sentence output.  "
                "Write the full text here when the goal says 'analyse', "
                "'write report', 'summarise', 'generate a natural language …', "
                "or similar.  Leave empty string when the goal only needs "
                "structured attribute/predicate updates."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": (
                "Internal chain-of-thought scratchpad.  Write your step-by-step "
                "reasoning here.  This field is stored for audit but never "
                "executed.  Keep it separate from 'prose' — 'prose' is the "
                "deliverable the user sees; 'reasoning' is your working."
            ),
        },
    },
    "required": ["attributes", "predicates"],
    "additionalProperties": False,
}

# Anthropic tool definition for forced structured output
_ROF_TOOL_DEFINITION: dict = {
    "name": "rof_graph_update",
    "description": (
        "Record the results of each workflow goal into the RelateLang graph. "
        "Always call this tool to respond — never return plain text. "
        "Use 'attributes' for structured values, 'predicates' for categorical "
        "conclusions, 'prose' for any free-form text output (reports, "
        "summaries, recommendations, analysis), and 'reasoning' for your "
        "internal chain-of-thought."
    ),
    "input_schema": ROF_GRAPH_UPDATE_SCHEMA,
}
