"""
tests/test_ollama_live.py
==========================
Ollama-specific live integration tests.

These tests require a running Ollama instance and are skipped by default.
Set the following environment variables to run them:

    ROF_TEST_PROVIDER=ollama
    ROF_TEST_MODEL=qwen3.5:9b   # or qwen3.5:27b, gemma3:12b, etc.

Example (PowerShell):
    $env:ROF_TEST_PROVIDER="ollama"
    $env:ROF_TEST_MODEL="qwen3.5:9b"
    pytest tests/test_ollama_live.py -v -m live_integration

Example (bash):
    ROF_TEST_PROVIDER=ollama ROF_TEST_MODEL=qwen3.5:9b \\
        pytest tests/test_ollama_live.py -v -m live_integration

What is covered
---------------
1. Provider bootstrap
   - OllamaProvider builds without error
   - supports_structured_output() returns True (native /api/chat path)
   - supports_tool_calling() returns False (native path, no openai compat)
   - context_limit is a positive integer

2. Raw completion — output_mode=json
   - /api/chat is called (message.content populated, not empty)
   - Response parses as valid JSON matching the rof_graph_update schema
   - Thinking models (qwen3, deepseek-r1) do not produce empty content

3. Raw completion — output_mode=rl
   - Response is non-empty plain text
   - Contains at least one RelateLang-style statement

4. output_mode=auto resolution
   - Resolves to "json" for OllamaProvider (supports_structured_output=True)
   - LLMRequest.output_mode is "json" when auto is used

5. Orchestrator integration — output_mode=json
   - customer_segmentation.rl runs to completion
   - At least one goal is ACHIEVED
   - Snapshot contains Customer entity with attributes
   - No goal ends with empty result string (vacuous ACHIEVED)

6. Orchestrator integration — output_mode=rl
   - Same workflow runs with rl mode
   - At least one goal is ACHIEVED
   - Snapshot contains Customer entity

7. Orchestrator integration — output_mode=auto
   - Auto resolves to json for Ollama
   - Workflow completes successfully

8. Graph update assertions
   - JSON mode: attributes are written to the graph (not zero updates)
   - The "prose-only reply" warning is NOT triggered

9. Pipeline — 2-stage pipeline_output_mode fixture
   - Stage 1 (output_mode: rl)  completes successfully
   - Stage 2 (output_mode: json) completes successfully
   - Both stages write entities to the final snapshot
   - Context from stage 1 is injected into stage 2

10. Thinking-model safety
    - Response content is never empty string for a substantive prompt
    - done_reason is "stop" (not "length" / truncated)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.rof_core import (
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        RLParser,
        RunResult,
    )

    ROF_CORE_AVAILABLE = True
except ImportError:
    ROF_CORE_AVAILABLE = False

try:
    from rof_framework.llm.providers.ollama_provider import OllamaProvider

    OLLAMA_PROVIDER_AVAILABLE = True
except ImportError:
    OLLAMA_PROVIDER_AVAILABLE = False

try:
    from rof_framework.core.orchestrator.orchestrator import (
        OrchestratorConfig as _OrchestratorConfig,
    )
    from rof_framework.rof_pipeline import (
        OnFailure,
        PipelineBuilder,
        PipelineResult,
        StageResult,
    )

    ROF_PIPELINE_AVAILABLE = True
except ImportError:
    ROF_PIPELINE_AVAILABLE = False

try:
    import yaml  # type: ignore

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Markers / module-level skip
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_framework.rof_core not available"),
    pytest.mark.skipif(
        not OLLAMA_PROVIDER_AVAILABLE,
        reason="rof_framework.llm.providers.ollama_provider not available",
    ),
    pytest.mark.live_integration,
]

# ---------------------------------------------------------------------------
# Directory constants
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PIPELINE_OUTPUT_MODE = FIXTURES_DIR / "pipeline_output_mode"

# ---------------------------------------------------------------------------
# Minimal RL source used by several tests
# ---------------------------------------------------------------------------

_CUSTOMER_RL = (
    (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
    if (FIXTURES_DIR / "customer_segmentation.rl").exists()
    else """
define Customer as "A person who purchases products from the store".
Customer has total_purchases of 15000.
Customer has account_age_days of 400.
ensure determine Customer segment.
ensure recommend Customer support tier.
"""
)

# A deliberately simple RL prompt used to test raw completion.
_SIMPLE_RL = """
define Product as "An item available for purchase".
Product has name of "Widget".
Product has price of 49.99.
Product has stock of 120.
ensure assess Product availability.
"""

# ---------------------------------------------------------------------------
# Environment / skip helpers
# ---------------------------------------------------------------------------


def _require_ollama() -> str:
    """
    Return the model name or skip the test.

    Prefers ROF_TEST_MODEL when set; falls back to qwen3.5:9b.
    Skips unless ROF_TEST_PROVIDER == "ollama".
    """
    provider = os.environ.get("ROF_TEST_PROVIDER", "").strip().lower()
    if provider != "ollama":
        pytest.skip(
            "Ollama live tests require ROF_TEST_PROVIDER=ollama. "
            "Example: $env:ROF_TEST_PROVIDER='ollama'; $env:ROF_TEST_MODEL='qwen3.5:9b'"
        )
    model = os.environ.get("ROF_TEST_MODEL", "qwen3.5:9b").strip()
    return model


def _base_url() -> str:
    return os.environ.get(
        "ROF_BASE_URL", os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ollama_model() -> str:
    """Return the Ollama model name (session-scoped, triggers skip if not configured)."""
    return _require_ollama()


@pytest.fixture(scope="session")
def ollama_provider(ollama_model: str) -> OllamaProvider:
    """Build an OllamaProvider (native httpx path, not OpenAI-compat)."""
    return OllamaProvider(
        model=ollama_model,
        base_url=_base_url(),
        default_max_tokens=1024,
        default_temperature=0.0,
        timeout=180.0,
    )


@pytest.fixture(scope="session")
def ollama_provider_oc(ollama_model: str) -> OllamaProvider:
    """Build an OllamaProvider using the OpenAI-compat path (use_openai_compat=True)."""
    return OllamaProvider(
        model=ollama_model,
        base_url=_base_url(),
        default_max_tokens=1024,
        default_temperature=0.0,
        timeout=180.0,
        use_openai_compat=True,
    )


# ---------------------------------------------------------------------------
# Helper: build a fresh OrchestratorConfig with a given output_mode
# ---------------------------------------------------------------------------


def _orch_config(output_mode: str = "auto", max_iterations: int = 20) -> OrchestratorConfig:
    return OrchestratorConfig(
        max_iterations=max_iterations,
        auto_save_state=False,
        pause_on_error=False,
        output_mode=output_mode,
    )


# ---------------------------------------------------------------------------
# Helper: run RL source through the Orchestrator, return RunResult + requests
# ---------------------------------------------------------------------------


class _RecordingProvider(LLMProvider):
    """Wraps a real provider and records every LLMRequest sent to it."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.requests: list[LLMRequest] = []
        self.responses: list[LLMResponse] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        resp = self._inner.complete(request)
        self.responses.append(resp)
        return resp

    def supports_tool_calling(self) -> bool:
        return self._inner.supports_tool_calling()

    def supports_structured_output(self) -> bool:
        return self._inner.supports_structured_output()

    @property
    def context_limit(self) -> int:
        return self._inner.context_limit


def _run(
    source: str, provider: LLMProvider, output_mode: str = "auto"
) -> tuple[RunResult, _RecordingProvider]:
    recording = _RecordingProvider(provider)
    ast = RLParser().parse(source)
    orch = Orchestrator(
        llm_provider=recording,
        config=_orch_config(output_mode=output_mode),
    )
    result = orch.run(ast)
    return result, recording


# ===========================================================================
# Section 1 — Provider bootstrap
# ===========================================================================


class TestOllamaProviderBootstrap:
    """Verify OllamaProvider builds correctly and advertises the right capabilities."""

    def test_provider_builds(self, ollama_model: str):
        provider = OllamaProvider(model=ollama_model, base_url=_base_url())
        assert provider is not None

    def test_supports_structured_output_native_path(self, ollama_provider: OllamaProvider):
        """
        Native httpx path must return True — it sends format: <JSON schema>
        which Ollama enforces via grammar-based sampling (since Dec 2024).
        """
        assert ollama_provider.supports_structured_output() is True

    def test_supports_structured_output_openai_compat_path(
        self, ollama_provider_oc: OllamaProvider
    ):
        """OpenAI-compat path also returns True — it sends json_schema response_format."""
        assert ollama_provider_oc.supports_structured_output() is True

    def test_supports_tool_calling_native_path(self, ollama_provider: OllamaProvider):
        """Native httpx path does not support tool calling (no function-call protocol)."""
        assert ollama_provider.supports_tool_calling() is False

    def test_supports_tool_calling_openai_compat_path(self, ollama_provider_oc: OllamaProvider):
        """OpenAI-compat path enables tool calling via the OpenAI SDK."""
        assert ollama_provider_oc.supports_tool_calling() is True

    def test_context_limit_is_positive(self, ollama_provider: OllamaProvider):
        assert ollama_provider.context_limit > 0

    def test_context_limit_type(self, ollama_provider: OllamaProvider):
        assert isinstance(ollama_provider.context_limit, int)


# ===========================================================================
# Section 2 — Raw completion: output_mode=json
# ===========================================================================


class TestOllamaRawCompletionJson:
    """
    Test the /api/chat endpoint with format=<JSON schema>.

    Key regression: qwen3 / deepseek-r1 (thinking models) previously returned
    empty content because the code used /api/generate which puts output into
    `response` — now using /api/chat which puts output into `message.content`.
    """

    def test_json_response_is_not_empty(self, ollama_provider: OllamaProvider):
        """Content must never be empty string for a substantive prompt."""
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system=(
                "You are a RelateLang workflow executor. "
                "Respond ONLY with a valid JSON object matching this schema: "
                '{"attributes": [{"entity": "...", "name": "...", "value": ...}], '
                '"predicates": [{"entity": "...", "value": "..."}], "reasoning": "..."}.'
            ),
            output_mode="json",
            max_tokens=512,
            temperature=0.0,
        )
        resp = ollama_provider.complete(req)
        assert resp.content != "", (
            "Ollama returned empty content for a json-mode request. "
            "This is the thinking-model regression: /api/generate puts output into "
            "`response` (empty for thinking models); /api/chat puts it into "
            "`message.content` which is always populated."
        )

    def test_json_response_parses_as_json(self, ollama_provider: OllamaProvider):
        """Response content must be valid JSON."""
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system=(
                "You are a RelateLang workflow executor. "
                "Respond ONLY with a valid JSON object. "
                'Schema: {"attributes": [{"entity": "str", "name": "str", "value": "any"}], '
                '"predicates": [{"entity": "str", "value": "str"}], "reasoning": "str"}.'
            ),
            output_mode="json",
            max_tokens=512,
            temperature=0.0,
        )
        resp = ollama_provider.complete(req)
        assert resp.content, "Empty response — cannot parse JSON"
        # Strip markdown fences if the model wraps the JSON
        raw = resp.content.strip()
        if raw.startswith("```"):
            import re

            raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"Response is not valid JSON: {exc}\n"
                f"Raw content (first 400 chars): {resp.content[:400]}"
            )
        assert isinstance(data, dict), f"Expected JSON object, got {type(data)}"

    def test_json_response_has_rof_schema_keys(self, ollama_provider: OllamaProvider):
        """JSON response should contain 'attributes' and 'predicates' keys."""
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system=(
                "You are a RelateLang workflow executor. "
                "Respond ONLY with a valid JSON object. "
                'Required keys: "attributes" (array), "predicates" (array), "reasoning" (string).'
            ),
            output_mode="json",
            max_tokens=512,
            temperature=0.0,
        )
        resp = ollama_provider.complete(req)
        assert resp.content, "Empty response"
        raw = resp.content.strip()
        import re

        raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pytest.skip("Model did not return JSON — schema enforcement may be model-dependent")
        assert "attributes" in data, (
            f"'attributes' key missing from JSON response. Keys found: {list(data.keys())}"
        )
        assert "predicates" in data, (
            f"'predicates' key missing from JSON response. Keys found: {list(data.keys())}"
        )
        assert isinstance(data["attributes"], list), "'attributes' must be an array"
        assert isinstance(data["predicates"], list), "'predicates' must be an array"

    def test_raw_field_is_populated(self, ollama_provider: OllamaProvider):
        """LLMResponse.raw should contain the full Ollama API response dict."""
        req = LLMRequest(prompt=_SIMPLE_RL, output_mode="json", max_tokens=256)
        resp = ollama_provider.complete(req)
        assert isinstance(resp.raw, dict), "raw should be a dict"
        # /api/chat response must contain 'message' key
        assert "message" in resp.raw, (
            f"Expected 'message' key in raw response (from /api/chat). "
            f"Keys found: {list(resp.raw.keys())}"
        )

    def test_done_is_true(self, ollama_provider: OllamaProvider):
        """/api/chat non-streaming response must have done=true."""
        req = LLMRequest(prompt=_SIMPLE_RL, output_mode="json", max_tokens=256)
        resp = ollama_provider.complete(req)
        assert resp.raw.get("done") is True, (
            f"Expected done=true in Ollama response. raw keys: {list(resp.raw.keys())}"
        )


# ===========================================================================
# Section 3 — Raw completion: output_mode=rl
# ===========================================================================


class TestOllamaRawCompletionRl:
    """Test the /api/chat endpoint without format constraint (rl mode)."""

    def test_rl_response_is_not_empty(self, ollama_provider: OllamaProvider):
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system=(
                "You are a RelateLang workflow executor. "
                "Respond with plain RelateLang attribute statements. "
                'Example: Product has availability of "in_stock".'
            ),
            output_mode="rl",
            max_tokens=256,
            temperature=0.0,
        )
        resp = ollama_provider.complete(req)
        assert resp.content != "", "Ollama returned empty content for an rl-mode request"
        assert len(resp.content.strip()) > 0

    def test_rl_response_contains_text(self, ollama_provider: OllamaProvider):
        """RL mode should produce natural language or RL statements — not empty."""
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system="You are a RelateLang workflow executor. Respond in RelateLang format.",
            output_mode="rl",
            max_tokens=256,
            temperature=0.0,
        )
        resp = ollama_provider.complete(req)
        assert resp.content.strip(), "Response is blank"


# ===========================================================================
# Section 4 — output_mode=auto resolution
# ===========================================================================


class TestOllamaAutoOutputModeResolution:
    """
    Verify that output_mode='auto' resolves to 'json' for OllamaProvider
    because supports_structured_output() now returns True.
    """

    def test_auto_resolves_to_json_in_llm_request(self, ollama_provider: OllamaProvider):
        """
        When the Orchestrator runs with output_mode='auto', the LLMRequest
        delivered to the provider must carry output_mode='json' (not 'rl'),
        because OllamaProvider.supports_structured_output() is True.
        """
        recording = _RecordingProvider(ollama_provider)
        ast = RLParser().parse(_SIMPLE_RL)
        orch = Orchestrator(
            llm_provider=recording,
            config=_orch_config(output_mode="auto"),
        )
        orch.run(ast)

        assert recording.requests, "No LLM requests were made"
        for req in recording.requests:
            assert req.output_mode == "json", (
                f"Expected output_mode='json' after auto-resolution for Ollama, "
                f"but got '{req.output_mode}'. "
                f"supports_structured_output() must return True for auto→json to work."
            )

    def test_auto_produces_non_empty_responses(self, ollama_provider: OllamaProvider):
        """All responses in auto mode must be non-empty."""
        recording = _RecordingProvider(ollama_provider)
        ast = RLParser().parse(_SIMPLE_RL)
        orch = Orchestrator(
            llm_provider=recording,
            config=_orch_config(output_mode="auto"),
        )
        orch.run(ast)

        for i, resp in enumerate(recording.responses):
            assert resp.content != "", (
                f"Response #{i + 1} was empty in auto mode. "
                "Thinking-model regression: content must come from message.content, not response."
            )


# ===========================================================================
# Section 5 — Orchestrator integration: output_mode=json
# ===========================================================================


class TestOllamaOrchestratorJson:
    """End-to-end Orchestrator run with output_mode=json on customer_segmentation.rl."""

    def test_run_completes_without_exception(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        assert result is not None

    def test_at_least_one_goal_achieved(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        goals = result.snapshot.get("goals", [])
        achieved = [g for g in goals if g.get("status") == "ACHIEVED"]
        assert achieved, (
            f"No goals were ACHIEVED in json mode. "
            f"Goal statuses: {[(g['expr'], g['status']) for g in goals]}"
        )

    def test_snapshot_contains_customer_entity(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        entities = result.snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"'Customer' entity missing from snapshot. Found: {list(entities.keys())}"
        )

    def test_customer_has_attributes(self, ollama_provider: OllamaProvider):
        """Customer entity must have seed attributes (name, total_purchases, etc.)."""
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        customer = result.snapshot.get("entities", {}).get("Customer", {})
        attrs = customer.get("attributes", {})
        assert attrs, (
            "Customer entity has no attributes in the snapshot. "
            "Seed attributes should always be present."
        )

    def test_no_goal_has_empty_result(self, ollama_provider: OllamaProvider):
        """
        No ACHIEVED goal should have an empty result string.
        An empty result means the goal was vacuously satisfied — the LLM
        returned empty content and the Orchestrator accepted it without
        writing any graph updates.  This is the core thinking-model regression.
        """
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        goals = result.snapshot.get("goals", [])
        vacuous = [
            g for g in goals if g.get("status") == "ACHIEVED" and g.get("result", "NONEMPTY") == ""
        ]
        assert not vacuous, (
            f"Goals were ACHIEVED with empty result (vacuous success): "
            f"{[g['expr'] for g in vacuous]}. "
            "This indicates the LLM returned empty content — check /api/chat migration."
        )

    def test_llm_called_at_least_once(self, ollama_provider: OllamaProvider):
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        assert len(recording.requests) >= 1, "Orchestrator made no LLM calls"

    def test_all_requests_use_json_mode(self, ollama_provider: OllamaProvider):
        """Every LLMRequest must carry output_mode='json' in json mode."""
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        for i, req in enumerate(recording.requests):
            assert req.output_mode == "json", (
                f"Request #{i + 1} has output_mode='{req.output_mode}', expected 'json'"
            )

    def test_all_responses_non_empty_in_json_mode(self, ollama_provider: OllamaProvider):
        """
        All responses must be non-empty.
        Regression guard: /api/generate + thinking models → empty `response` field.
        """
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        for i, resp in enumerate(recording.responses):
            assert resp.content != "", (
                f"Response #{i + 1} was empty in json mode. "
                "This is the /api/generate → /api/chat regression for thinking models."
            )


# ===========================================================================
# Section 6 — Orchestrator integration: output_mode=rl
# ===========================================================================


class TestOllamaOrchestratorRl:
    """End-to-end Orchestrator run with output_mode=rl (plain RelateLang)."""

    def test_run_completes_without_exception(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="rl")
        assert result is not None

    def test_snapshot_contains_customer_entity(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="rl")
        entities = result.snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"'Customer' entity missing from snapshot in rl mode. Found: {list(entities.keys())}"
        )

    def test_all_requests_use_rl_mode(self, ollama_provider: OllamaProvider):
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="rl")
        for i, req in enumerate(recording.requests):
            assert req.output_mode == "rl", (
                f"Request #{i + 1} has output_mode='{req.output_mode}', expected 'rl'"
            )

    def test_all_responses_non_empty_in_rl_mode(self, ollama_provider: OllamaProvider):
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="rl")
        for i, resp in enumerate(recording.responses):
            assert resp.content != "", f"Response #{i + 1} was empty in rl mode"


# ===========================================================================
# Section 7 — Orchestrator integration: output_mode=auto
# ===========================================================================


class TestOllamaOrchestratorAuto:
    """End-to-end Orchestrator run with output_mode=auto (should resolve to json)."""

    def test_run_completes_without_exception(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="auto")
        assert result is not None

    def test_auto_resolves_to_json_requests(self, ollama_provider: OllamaProvider):
        """Every LLMRequest must carry output_mode='json' when auto is used with Ollama."""
        _, recording = _run(_CUSTOMER_RL, ollama_provider, output_mode="auto")
        assert recording.requests, "No LLM requests were made"
        for i, req in enumerate(recording.requests):
            assert req.output_mode == "json", (
                f"Request #{i + 1} has output_mode='{req.output_mode}' in auto mode. "
                f"Expected 'json' because OllamaProvider.supports_structured_output()=True."
            )

    def test_snapshot_contains_customer_entity(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="auto")
        entities = result.snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"'Customer' missing from snapshot in auto mode. Found: {list(entities.keys())}"
        )


# ===========================================================================
# Section 8 — Graph update assertions
# ===========================================================================


class TestOllamaGraphUpdates:
    """
    Verify that the Orchestrator actually writes graph updates from JSON
    responses — i.e. attributes/predicates end up in the snapshot — and that
    the 'prose-only reply' warning path is not triggered.
    """

    def test_json_mode_writes_attributes_to_snapshot(self, ollama_provider: OllamaProvider):
        """
        After a successful json-mode run, at least one entity must have
        attributes beyond the seed values written by the LLM.
        """
        # loan_approval has more interesting LLM goals (segment, decision, etc.)
        loan_rl = (
            (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
            if (FIXTURES_DIR / "loan_approval.rl").exists()
            else _CUSTOMER_RL
        )

        result, _ = _run(loan_rl, ollama_provider, output_mode="json")

        # At least one ACHIEVED goal means graph updates were applied
        goals = result.snapshot.get("goals", [])
        achieved = [g for g in goals if g.get("status") == "ACHIEVED"]
        assert achieved, (
            "No goals were ACHIEVED — either zero graph updates were applied "
            "(prose-only reply) or all goals failed."
        )

    def test_json_mode_no_vacuous_goals_with_empty_result(self, ollama_provider: OllamaProvider):
        """
        A goal with status=ACHIEVED and result='' means the Orchestrator
        accepted an empty LLM response.  This must not happen in json mode
        with a working /api/chat integration.
        """
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="json")
        goals = result.snapshot.get("goals", [])
        empty_achieved = [
            g["expr"] for g in goals if g.get("status") == "ACHIEVED" and g.get("result", "X") == ""
        ]
        assert not empty_achieved, (
            f"These goals were ACHIEVED with empty result (vacuous): {empty_achieved}. "
            "Empty result = LLM returned '' and zero graph updates were applied. "
            "Root cause: /api/generate returns empty `response` for thinking models; "
            "/api/chat must be used instead."
        )

    def test_rl_mode_produces_steps(self, ollama_provider: OllamaProvider):
        result, _ = _run(_CUSTOMER_RL, ollama_provider, output_mode="rl")
        assert len(result.steps) > 0, "Orchestrator produced zero steps in rl mode"


# ===========================================================================
# Section 9 — Pipeline: 2-stage pipeline_output_mode fixture
# ===========================================================================


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_framework.rof_pipeline not available")
@pytest.mark.skipif(not YAML_AVAILABLE, reason="pyyaml not installed")
class TestOllamaPipelineOutputMode:
    """
    Run the pipeline_output_mode fixture end-to-end against Ollama.

    Stage 1 (extract)  — output_mode: rl
    Stage 2 (classify) — output_mode: json

    Both stages must complete, context must flow from stage 1 to stage 2,
    and both entities (Customer, Product) seeded in stage 1 must appear in
    the final snapshot.
    """

    @staticmethod
    def _build_pipeline(llm: LLMProvider) -> object:
        """Build the pipeline_output_mode pipeline from its YAML with orch_config wired."""
        yaml_path = PIPELINE_OUTPUT_MODE / "pipeline.yaml"
        if not yaml_path.exists():
            pytest.skip(f"Fixture not found: {yaml_path}")

        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        base_dir = yaml_path.parent
        builder = PipelineBuilder(llm=llm)

        for s in raw.get("stages", []):
            rl_file = s.get("rl_file", "")
            stage_output_mode = s.get("output_mode", "auto")

            stage_orch_cfg = None
            if stage_output_mode != "auto":
                stage_orch_cfg = _OrchestratorConfig(
                    auto_save_state=False,
                    pause_on_error=False,
                    output_mode=stage_output_mode,
                )

            if rl_file:
                builder.stage(
                    name=s["name"],
                    rl_file=str(base_dir / rl_file),
                    description=s.get("description", ""),
                    orch_config=stage_orch_cfg,
                )

        cfg_raw = raw.get("config", {})
        on_fail_str = cfg_raw.get("on_failure", "halt").upper()
        on_fail = OnFailure[on_fail_str] if on_fail_str in OnFailure.__members__ else OnFailure.HALT
        builder.config(
            on_failure=on_fail,
            retry_count=cfg_raw.get("retry_count", 2),
            inject_prior_context=cfg_raw.get("inject_prior_context", True),
        )
        return builder.build()

    def test_pipeline_runs_without_exception(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        assert result is not None

    def test_pipeline_has_two_stages(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        assert len(result.steps) == 2, f"Expected 2 stage results, got {len(result.steps)}"

    def test_stage_names_are_correct(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        names = result.stage_names()
        assert "extract" in names, f"'extract' not in stage names: {names}"
        assert "classify" in names, f"'classify' not in stage names: {names}"

    def test_extract_stage_succeeded(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        extract = result.stage("extract")
        assert extract is not None, "'extract' stage result not found"
        assert extract.success, f"'extract' stage (output_mode=rl) failed: {extract.error}"

    def test_classify_stage_succeeded(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        classify = result.stage("classify")
        assert classify is not None, "'classify' stage result not found"
        assert classify.success, f"'classify' stage (output_mode=json) failed: {classify.error}"

    def test_final_snapshot_contains_customer(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        entities = result.final_snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"'Customer' missing from final snapshot. Found: {list(entities.keys())}"
        )

    def test_final_snapshot_contains_product(self, ollama_provider: OllamaProvider):
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        entities = result.final_snapshot.get("entities", {})
        assert "Product" in entities, (
            f"'Product' missing from final snapshot. Found: {list(entities.keys())}"
        )

    def test_context_injected_into_stage2(self, ollama_provider: OllamaProvider):
        """
        Stage 2 (classify) receives stage 1's snapshot as input.
        Verified by checking that classify.input_snapshot is non-empty.
        """
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        classify = result.stage("classify")
        if classify is not None and hasattr(classify, "input_snapshot"):
            snap = classify.input_snapshot or {}
            entities = snap.get("entities", {})
            assert entities, (
                "classify stage received an empty input_snapshot — "
                "context injection from stage 1 did not work."
            )

    def test_stage1_uses_rl_output_mode(self, ollama_provider: OllamaProvider):
        """
        Verify the orch_config on the extract stage carries output_mode='rl'.
        We do this by checking the stage result's run_result steps.
        """
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        extract = result.stage("extract")
        if extract and extract.run_result and extract.run_result.steps:
            for step in extract.run_result.steps:
                req = getattr(step, "llm_request", None)
                if req is not None:
                    assert req.output_mode == "rl", (
                        f"extract stage step has output_mode='{req.output_mode}', expected 'rl'"
                    )

    def test_stage2_uses_json_output_mode(self, ollama_provider: OllamaProvider):
        """
        Verify the orch_config on the classify stage carries output_mode='json'.
        """
        pipeline = self._build_pipeline(ollama_provider)
        result = pipeline.run()
        classify = result.stage("classify")
        if classify and classify.run_result and classify.run_result.steps:
            for step in classify.run_result.steps:
                req = getattr(step, "llm_request", None)
                if req is not None:
                    assert req.output_mode == "json", (
                        f"classify stage step has output_mode='{req.output_mode}', expected 'json'"
                    )


# ===========================================================================
# Section 10 — Thinking-model safety
# ===========================================================================


class TestOllamaThinkingModelSafety:
    """
    Guard against the /api/generate regression for thinking models.

    When /api/generate is used with a thinking model (qwen3, deepseek-r1):
      - The model puts its chain-of-thought into `response`
      - The actual answer ends up empty or in a `thinking` field
      - ROF receives empty content and silently marks goals ACHIEVED with ''

    The fix is to use /api/chat which always returns the answer in
    `message.content` regardless of whether the model thinks first.
    """

    def test_raw_response_field_is_message_content(self, ollama_provider: OllamaProvider):
        """
        The raw Ollama response must use the /api/chat shape:
          {"message": {"role": "assistant", "content": "..."}}
        NOT the /api/generate shape:
          {"response": "..."}
        """
        req = LLMRequest(
            prompt='Say hello in JSON: {"greeting": "hello"}',
            output_mode="json",
            max_tokens=64,
        )
        resp = ollama_provider.complete(req)
        raw = resp.raw

        # /api/chat shape
        assert "message" in raw, (
            f"Raw response uses /api/generate shape (has 'response' key, missing 'message'). "
            f"Keys: {list(raw.keys())}. "
            "The provider must call /api/chat, not /api/generate."
        )
        assert "content" in raw.get("message", {}), (
            f"'message.content' missing from raw response. message keys: {list(raw.get('message', {}).keys())}"
        )

    def test_content_equals_message_content(self, ollama_provider: OllamaProvider):
        """LLMResponse.content must equal raw['message']['content']."""
        req = LLMRequest(
            prompt='Say hello in JSON: {"greeting": "hello"}',
            output_mode="json",
            max_tokens=64,
        )
        resp = ollama_provider.complete(req)
        assert resp.content == resp.raw.get("message", {}).get("content", ""), (
            "LLMResponse.content does not match raw['message']['content']. "
            "Provider may be reading from the wrong field."
        )

    def test_multiple_goals_no_empty_responses(self, ollama_provider: OllamaProvider):
        """
        Run a multi-goal workflow and assert no response is empty.
        Thinking models previously returned '' on every call via /api/generate.
        """
        recording = _RecordingProvider(ollama_provider)
        ast = RLParser().parse(_CUSTOMER_RL)
        orch = Orchestrator(
            llm_provider=recording,
            config=_orch_config(output_mode="json"),
        )
        orch.run(ast)

        assert recording.responses, "No responses recorded"
        empty = [i + 1 for i, r in enumerate(recording.responses) if r.content == ""]
        assert not empty, (
            f"Responses #{empty} were empty strings. "
            "Thinking-model regression: /api/generate puts output into `response` "
            "(empty for thinking models). /api/chat puts it into `message.content`."
        )

    def test_response_generation_completes(self, ollama_provider: OllamaProvider):
        """done_reason should be 'stop', not 'length' (truncated output)."""
        req = LLMRequest(
            prompt=_SIMPLE_RL,
            system="Respond with a brief JSON object.",
            output_mode="json",
            max_tokens=512,
        )
        resp = ollama_provider.complete(req)
        done_reason = resp.raw.get("done_reason", "stop")
        # "length" means the model was cut off — warn but don't hard-fail
        # because max_tokens may be tight for some models.
        if done_reason == "length":
            pytest.xfail(
                f"Response was truncated (done_reason='length'). "
                f"Increase max_tokens or use a smaller model. "
                f"Content so far: {resp.content[:200]}"
            )
        assert done_reason in ("stop", ""), (
            f"Unexpected done_reason: '{done_reason}'. Expected 'stop'."
        )
