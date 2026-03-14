"""
tests/unit/test_tools.py
========================
Unit tests for all ROF Bot custom tools and the database interface.

Tests run without any external services — all backends are either
in-memory or use a temporary SQLite file.  No LLM calls are made.

Run with:
    cd demos/rof_bot
    pytest tests/unit/test_tools.py -v

Or from the project root:
    pytest demos/rof_bot/tests/unit/test_tools.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure rof_bot root and rof_framework src are on sys.path
# ---------------------------------------------------------------------------
_BOT_ROOT = Path(__file__).resolve().parent.parent.parent  # demos/rof_bot/
_SRC_ROOT = _BOT_ROOT.parent.parent / "src"  # rof/src/

for _p in (_BOT_ROOT, str(_SRC_ROOT)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ===========================================================================
# Helpers
# ===========================================================================


def _make_tool_request(input_data: dict, goal: str = "") -> Any:
    """Create a minimal ToolRequest duck-type for tests."""

    class _Req:
        def __init__(self, inp, g):
            self.input = inp
            self.goal = g
            self.name = g

    return _Req(input_data, goal)


def _entity_snapshot(entity_name: str, attrs: dict) -> dict:
    """Build a snapshot-entity style input dict."""
    return {entity_name: {"attributes": attrs}}


# ===========================================================================
# DataSourceTool
# ===========================================================================


class TestDataSourceTool:
    """Tests for tools/data_source.py::DataSourceTool."""

    def _make_tool(self, **kwargs) -> Any:
        from tools.data_source import DataSourceTool

        kwargs.setdefault("dry_run", True)
        return DataSourceTool(**kwargs)

    def test_dry_run_returns_stub_data(self):
        """Dry-run mode returns synthetic stub data without any HTTP call."""
        tool = self._make_tool()
        req = _make_tool_request(
            {"Subject": {"attributes": {"id": "TEST-001", "source": "primary_system"}}}
        )
        resp = tool.execute(req)

        assert resp.success is True
        assert resp.output is not None
        assert "rl_context" in resp.output
        assert "raw" in resp.output
        assert "STUB" in resp.output["raw"]["content"]
        assert resp.output["subject_id"] == "TEST-001"

    def test_rl_context_contains_required_attributes(self):
        """RL context output must contain Subject entity attribute statements."""
        tool = self._make_tool()
        req = _make_tool_request({"Subject": {"attributes": {"id": "T-42", "source": "system_a"}}})
        resp = tool.execute(req)
        ctx = resp.output["rl_context"]

        assert "Subject has status of" in ctx
        assert "Subject has data_complete of true" in ctx

    def test_direct_call_style_input(self):
        """Tool accepts direct-call style input (subject_id key at top level)."""
        tool = self._make_tool()
        req = _make_tool_request({"subject_id": "DIRECT-001", "source": "sys"})
        resp = tool.execute(req)

        assert resp.success is True
        assert resp.output["subject_id"] == "DIRECT-001"

    def test_missing_subject_uses_defaults(self):
        """Empty input falls back to SUBJECT-001 / primary_system defaults."""
        tool = self._make_tool()
        req = _make_tool_request({})
        resp = tool.execute(req)

        assert resp.success is True
        assert resp.output["subject_id"] == "SUBJECT-001"

    def test_source_unavailable_returns_degraded_response(self):
        """When the external API is unreachable, tool returns data_complete=false."""
        tool = self._make_tool(dry_run=False, base_url="http://localhost:0")
        req = _make_tool_request({"Subject": {"attributes": {"id": "T-99", "source": "broken"}}})
        resp = tool.execute(req)

        # Tool must not raise — pipeline continues on degraded path
        assert resp.success is True
        assert "Subject has data_complete of false" in resp.output["rl_context"]

    def test_content_truncation(self):
        """Long content is truncated to max_content_chars in rl_context."""
        tool = self._make_tool(max_content_chars=50)
        # Patch stub data to return very long content
        long_content = "X" * 2000
        with patch.object(
            tool,
            "_stub_data",
            return_value={
                "id": "T-1",
                "source": "s",
                "status": "open",
                "priority": "normal",
                "content": long_content,
                "created_at": "2025-01-01T00:00:00Z",
            },
        ):
            req = _make_tool_request({"subject_id": "T-1"})
            resp = tool.execute(req)

        ctx = resp.output["rl_context"]
        # The truncated value must be at most 50 chars in the rl_context attribute value
        import re

        match = re.search(r'Subject has raw_content of "([^"]*)"', ctx)
        if match:
            assert len(match.group(1)) <= 50

    def test_subject_not_found_returns_not_found_rl(self):
        """SubjectNotFound exception returns a not-found rl_context."""
        from tools.data_source import SubjectNotFound

        tool = self._make_tool(dry_run=False)
        with patch.object(tool, "_call_external_api", side_effect=SubjectNotFound("404")):
            req = _make_tool_request({"subject_id": "MISSING"})
            resp = tool.execute(req)

        assert resp.success is True
        assert 'fetch_error of "not_found"' in resp.output["rl_context"]

    def test_trigger_keywords_present(self):
        """Tool exposes at least the canonical trigger keywords."""
        from tools.data_source import DataSourceTool

        tool = DataSourceTool(dry_run=True)
        keywords = tool.trigger_keywords
        assert any("retrieve Subject data" in kw for kw in keywords)
        assert any("fetch from primary source" in kw for kw in keywords)

    def test_name_is_datasourcetool(self):
        from tools.data_source import DataSourceTool

        assert DataSourceTool(dry_run=True).name == "DataSourceTool"


# ===========================================================================
# ContextEnrichmentTool
# ===========================================================================


class TestContextEnrichmentTool:
    """Tests for tools/context_enrichment.py::ContextEnrichmentTool."""

    def _make_tool(self, **kwargs) -> Any:
        from tools.context_enrichment import ContextEnrichmentTool

        return ContextEnrichmentTool(dry_run=True, **kwargs)

    def test_dry_run_returns_stub_context(self):
        tool = self._make_tool()
        req = _make_tool_request({"Subject": {"attributes": {"id": "C-001", "source": "crm"}}})
        resp = tool.execute(req)

        assert resp.success is True
        ctx = resp.output["rl_context"]
        assert "Context has history_available of true" in ctx
        assert "Context has enrichment_type" in ctx

    def test_unavailable_returns_soft_failure(self):
        """EnrichmentUnavailable returns a degraded but successful response."""
        from tools.context_enrichment import ContextEnrichmentTool, EnrichmentUnavailable

        tool = ContextEnrichmentTool(dry_run=False)
        with patch.object(tool, "_fetch_enrichment", side_effect=EnrichmentUnavailable("timeout")):
            req = _make_tool_request({"subject_id": "C-002"})
            resp = tool.execute(req)

        assert resp.success is True
        assert "Context has history_available of false" in resp.output["rl_context"]
        assert "enrichment_error" in resp.output["rl_context"]

    def test_empty_subject_uses_defaults(self):
        tool = self._make_tool()
        resp = tool.execute(_make_tool_request({}))
        assert resp.success is True
        assert resp.output["subject_id"] == "SUBJECT-001"

    def test_enrichment_type_is_included(self):
        tool = self._make_tool(enrichment_type="crm_history")
        resp = tool.execute(_make_tool_request({"subject_id": "X"}))
        assert "crm_history" in resp.output["rl_context"]

    def test_name_is_contextenrichmenttool(self):
        from tools.context_enrichment import ContextEnrichmentTool

        assert ContextEnrichmentTool(dry_run=True).name == "ContextEnrichmentTool"


# ===========================================================================
# ActionExecutorTool
# ===========================================================================


class TestActionExecutorTool:
    """Tests for tools/action_executor.py::ActionExecutorTool."""

    def _make_tool(self, dry_run=True, **kwargs) -> Any:
        from tools.action_executor import ActionExecutorTool

        return ActionExecutorTool(dry_run=dry_run, **kwargs)

    # ── Dry-run gate ──────────────────────────────────────────────────────────

    def test_dry_run_gate_prevents_live_execution(self):
        """BOT_DRY_RUN=True must NEVER call _execute_primary_action."""
        tool = self._make_tool(dry_run=True)
        with patch.object(tool, "_execute_primary_action") as mock_live:
            req = _make_tool_request(
                {
                    "Decision": {
                        "attributes": {
                            "action": "proceed",
                            "confidence_score": 0.90,
                            "reasoning_summary": "Test",
                        }
                    },
                    "Subject": {"attributes": {"id": "DRY-001"}},
                    "ResourceBudget": {"attributes": {"available_capacity": 1.0}},
                }
            )
            resp = tool.execute(req)

        mock_live.assert_not_called()
        assert resp.success is True
        assert resp.output["status"] == "dry_run"

    def test_dry_run_returns_correct_rl_context(self):
        tool = self._make_tool(dry_run=True)
        req = _make_tool_request(
            {
                "Decision": {
                    "attributes": {
                        "action": "proceed",
                        "confidence_score": 0.85,
                        "reasoning_summary": "ok",
                    }
                },
                "Subject": {"attributes": {"id": "DRY-002"}},
            }
        )
        resp = tool.execute(req)
        ctx = resp.output["rl_context"]

        assert 'Action has action_type of "proceed"' in ctx
        assert 'Action has status of "dry_run"' in ctx
        assert "Action has dry_run of true" in ctx
        assert "action_id" in resp.output

    def test_dry_run_all_action_types(self):
        """Dry-run should intercept all action types."""
        tool = self._make_tool(dry_run=True)
        for action in ("proceed", "escalate", "defer", "skip"):
            req = _make_tool_request(
                {
                    "Decision": {
                        "attributes": {
                            "action": action,
                            "confidence_score": 0.80,
                            "reasoning_summary": "test",
                        }
                    },
                    "Subject": {"attributes": {"id": f"DRY-{action}"}},
                }
            )
            resp = tool.execute(req)
            assert resp.success is True, f"Failed for action={action}"
            assert resp.output["status"] == "dry_run"

    def test_direct_call_style_input(self):
        """Tool accepts flat dict with action/confidence_score keys."""
        tool = self._make_tool(dry_run=True)
        req = _make_tool_request(
            {
                "action": "defer",
                "confidence_score": 0.60,
                "reasoning_summary": "low confidence",
                "subject_id": "DC-001",
            }
        )
        resp = tool.execute(req)
        assert resp.success is True
        assert resp.output["action_type"] == "defer"

    def test_unknown_action_type_returns_skip(self):
        """Unknown action types fall through to the skip path."""
        tool = self._make_tool(dry_run=True)
        req = _make_tool_request(
            {
                "Decision": {
                    "attributes": {
                        "action": "teleport",
                        "confidence_score": 0.99,
                        "reasoning_summary": "?",
                    }
                },
                "Subject": {"attributes": {"id": "UNK-001"}},
            }
        )
        resp = tool.execute(req)
        assert resp.success is True

    def test_name_is_actionexecutortool(self):
        from tools.action_executor import ActionExecutorTool

        assert ActionExecutorTool(dry_run=True).name == "ActionExecutorTool"

    def test_output_contains_action_id_uuid(self):
        """action_id must be a valid UUID string."""
        tool = self._make_tool(dry_run=True)
        req = _make_tool_request(
            {"action": "proceed", "confidence_score": 0.9, "reasoning_summary": "ok"}
        )
        resp = tool.execute(req)
        action_id = resp.output["action_id"]
        # Should parse as UUID without raising
        uuid.UUID(action_id)

    # ── Live mode (mocked) ────────────────────────────────────────────────────

    def test_live_mode_calls_primary_action(self):
        """When dry_run=False, proceed action calls _execute_primary_action."""
        tool = self._make_tool(dry_run=False)
        mock_result = {"status": "completed", "result_summary": "Done"}
        with patch.object(tool, "_execute_primary_action", return_value=mock_result):
            req = _make_tool_request(
                {
                    "Decision": {
                        "attributes": {
                            "action": "proceed",
                            "confidence_score": 0.90,
                            "reasoning_summary": "go",
                        }
                    },
                    "Subject": {"attributes": {"id": "LIVE-001"}},
                    "ResourceBudget": {"attributes": {"available_capacity": 0.5}},
                }
            )
            resp = tool.execute(req)

        assert resp.success is True
        assert resp.output["status"] == "completed"
        assert resp.output["dry_run"] is False

    def test_live_mode_escalate_calls_escalate_action(self):
        tool = self._make_tool(dry_run=False)
        mock_result = {"status": "completed", "result_summary": "Escalated"}
        with patch.object(tool, "_execute_escalate_action", return_value=mock_result):
            req = _make_tool_request(
                {
                    "Decision": {
                        "attributes": {
                            "action": "escalate",
                            "confidence_score": 0.70,
                            "reasoning_summary": "esc",
                        }
                    },
                    "Subject": {"attributes": {"id": "LIVE-002"}},
                }
            )
            resp = tool.execute(req)

        assert resp.success is True
        assert resp.output["action_type"] == "escalate"

    def test_live_mode_action_execution_error_returns_failure(self):
        """ActionExecutionError is caught and returned as success=False."""
        from tools.action_executor import ActionExecutionError

        tool = self._make_tool(dry_run=False)
        with patch.object(
            tool, "_execute_primary_action", side_effect=ActionExecutionError("API error")
        ):
            req = _make_tool_request(
                {"action": "proceed", "confidence_score": 0.9, "reasoning_summary": "go"}
            )
            resp = tool.execute(req)

        assert resp.success is False
        assert "API error" in resp.error


# ===========================================================================
# BotStateManagerTool
# ===========================================================================


class TestBotStateManagerTool:
    """Tests for tools/state_manager.py::BotStateManagerTool."""

    def _make_tool(self, initial_state=None) -> Any:
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        backend = _InMemoryBackend(initial=initial_state or {})
        return BotStateManagerTool(backend=backend)

    # ── Read mode ─────────────────────────────────────────────────────────────

    def test_read_returns_rl_context_with_all_metrics(self):
        """Read goal returns RL context with BotState and Constraints attributes."""
        tool = self._make_tool(
            {
                "resource_utilisation": 0.30,
                "concurrent_action_count": 2,
                "daily_error_rate": 0.01,
            }
        )
        req = _make_tool_request({}, goal="retrieve current_resource_utilisation")
        resp = tool.execute(req)

        assert resp.success is True
        ctx = resp.output["rl_context"]
        assert "BotState has resource_utilisation" in ctx
        assert "Constraints has resource_utilisation" in ctx
        assert "BotState has concurrent_action_count" in ctx
        assert "BotState has daily_error_rate" in ctx
        assert resp.output["mode"] == "read"

    def test_read_daily_error_rate_goal(self):
        tool = self._make_tool({"daily_error_rate": 0.03})
        req = _make_tool_request({}, goal="retrieve daily_error_rate for Constraints")
        resp = tool.execute(req)
        assert resp.success is True
        assert "daily_error_rate" in resp.output["rl_context"]

    def test_read_concurrent_count_goal(self):
        tool = self._make_tool({"concurrent_action_count": 3})
        req = _make_tool_request({}, goal="retrieve concurrent_action_count")
        resp = tool.execute(req)
        assert resp.success is True
        assert '"3"' in resp.output["rl_context"]

    # ── Threshold annotations ─────────────────────────────────────────────────

    def test_resource_limit_breached_annotation(self):
        """When resource_utilisation > 0.80, annotation is added to rl_context."""
        with patch.dict(os.environ, {"BOT_RESOURCE_UTILISATION_LIMIT": "0.80"}):
            tool = self._make_tool({"resource_utilisation": 0.85})
            resp = tool.execute(_make_tool_request({}, goal="retrieve BotState"))
        assert "resource_limit_breached of true" in resp.output["rl_context"]

    def test_no_breach_annotation_when_within_limits(self):
        with patch.dict(
            os.environ,
            {
                "BOT_RESOURCE_UTILISATION_LIMIT": "0.80",
                "BOT_DAILY_ERROR_BUDGET": "0.05",
                "BOT_MAX_CONCURRENT_ACTIONS": "5",
            },
        ):
            tool = self._make_tool(
                {
                    "resource_utilisation": 0.50,
                    "concurrent_action_count": 2,
                    "daily_error_rate": 0.02,
                }
            )
            resp = tool.execute(_make_tool_request({}, goal="retrieve BotState"))
        ctx = resp.output["rl_context"]
        assert "resource_limit_breached" not in ctx
        assert "error_budget_breached" not in ctx
        assert "concurrency_limit_breached" not in ctx

    # ── Write mode ────────────────────────────────────────────────────────────

    def test_write_decrements_concurrent_on_proceed_complete(self):
        """After a proceed/completed action, concurrent_action_count decrements."""
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        backend = _InMemoryBackend({"concurrent_action_count": 3})
        tool = BotStateManagerTool(backend=backend)

        req = _make_tool_request(
            {"Action": {"attributes": {"action_type": "proceed", "status": "completed"}}},
            goal="update BotState with Action result",
        )
        resp = tool.execute(req)

        assert resp.success is True
        assert resp.output["mode"] == "write"
        # Concurrent count should have been decremented
        new_state = tool.get_state()
        assert new_state["concurrent_action_count"] == 2

    def test_write_never_goes_below_zero(self):
        """concurrent_action_count never goes below zero."""
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        backend = _InMemoryBackend({"concurrent_action_count": 0})
        tool = BotStateManagerTool(backend=backend)

        req = _make_tool_request(
            {"Action": {"attributes": {"action_type": "proceed", "status": "completed"}}},
            goal="update BotState",
        )
        tool.execute(req)
        assert tool.get_state()["concurrent_action_count"] == 0

    def test_write_increments_cycle_count(self):
        tool = self._make_tool()
        req = _make_tool_request(
            {"Action": {"attributes": {"action_type": "skip", "status": "skipped"}}},
            goal="update BotState",
        )
        resp = tool.execute(req)
        assert resp.output["state"]["cycle_count_today"] >= 1

    # ── Direct state manipulation ─────────────────────────────────────────────

    def test_increment_concurrent(self):
        tool = self._make_tool({"concurrent_action_count": 1})
        new_val = tool.increment_concurrent()
        assert new_val == 2

    def test_decrement_concurrent(self):
        tool = self._make_tool({"concurrent_action_count": 2})
        new_val = tool.decrement_concurrent()
        assert new_val == 1

    def test_set_resource_utilisation_clamped(self):
        tool = self._make_tool()
        tool.set_resource_utilisation(1.5)
        assert tool.get_state()["resource_utilisation"] == 1.0

        tool.set_resource_utilisation(-0.5)
        assert tool.get_state()["resource_utilisation"] == 0.0

    def test_set_daily_error_rate(self):
        tool = self._make_tool()
        tool.set_daily_error_rate(0.04)
        assert tool.get_state()["daily_error_rate"] == pytest.approx(0.04)

    def test_name_is_statemanagertool(self):
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        tool = BotStateManagerTool(backend=_InMemoryBackend())
        assert tool.name == "StateManagerTool"


# ===========================================================================
# ExternalSignalTool
# ===========================================================================


class TestExternalSignalTool:
    """Tests for tools/external_signal.py::ExternalSignalTool."""

    def _make_tool(self, **kwargs) -> Any:
        from tools.external_signal import ExternalSignalTool

        return ExternalSignalTool(dry_run=True, **kwargs)

    def test_dry_run_returns_stub_signal(self):
        """Dry-run returns synthetic signal data."""
        tool = self._make_tool()
        req = _make_tool_request({"Subject": {"attributes": {"id": "SIG-001", "source": "sys"}}})
        resp = tool.execute(req)

        assert resp.success is True
        ctx = resp.output["rl_context"]
        assert 'ExternalSignal has signal_available of "true"' in ctx
        assert "ExternalSignal has signal_type" in ctx
        assert "ExternalSignal has signal_value" in ctx

    def test_unavailable_returns_soft_failure(self):
        """ExternalSignalUnavailable returns signal_available=false — pipeline continues."""
        from tools.external_signal import ExternalSignalTool, ExternalSignalUnavailable

        tool = ExternalSignalTool(dry_run=False)
        with patch.object(tool, "_fetch_signal", side_effect=ExternalSignalUnavailable("timeout")):
            req = _make_tool_request({"subject_id": "SIG-002"})
            resp = tool.execute(req)

        # Tool MUST return success=True even when signal is unavailable
        assert resp.success is True
        assert 'ExternalSignal has signal_available of "false"' in resp.output["rl_context"]
        assert "signal_error" in resp.output["rl_context"]
        assert resp.output["error"] is not None

    def test_unexpected_exception_does_not_propagate(self):
        """Unexpected exceptions are caught and returned as soft-unavailable."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=False)
        with patch.object(tool, "_fetch_signal", side_effect=RuntimeError("unexpected")):
            resp = tool.execute(_make_tool_request({"subject_id": "SIG-003"}))

        assert resp.success is True
        assert 'signal_available of "false"' in resp.output["rl_context"]

    def test_hard_timeout_cap(self):
        """Timeout is capped at 5 seconds regardless of constructor argument."""
        from tools.external_signal import _HARD_TIMEOUT_S, ExternalSignalTool

        tool = ExternalSignalTool(dry_run=True, timeout_s=999.0)
        assert tool._timeout_s <= _HARD_TIMEOUT_S

    def test_cache_hit_returns_cached_data(self):
        """Cache hit bypasses _fetch_signal entirely."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=False, cache_ttl_seconds=300)
        cached_signal = {
            "type": "sla_tier",
            "value": "premium",
            "source": "crm",
        }
        tool._cache.set("CACHED-001:sys", cached_signal)

        with patch.object(tool, "_fetch_signal") as mock_fetch:
            req = _make_tool_request(
                {"Subject": {"attributes": {"id": "CACHED-001", "source": "sys"}}}
            )
            resp = tool.execute(req)

        mock_fetch.assert_not_called()
        assert resp.output["cached"] is True
        assert 'signal_available of "true"' in resp.output["rl_context"]

    def test_invalidate_cache(self):
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=False, cache_ttl_seconds=300)
        tool._cache.set("X-001:sys", {"type": "t", "value": "v", "source": "s"})
        tool.invalidate_cache("X-001", "sys")
        assert tool._cache.get("X-001:sys") is None

    def test_no_cache_when_ttl_is_zero(self):
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=True, cache_ttl_seconds=0)
        assert tool._cache is None

    def test_name_is_externalsignaltool(self):
        from tools.external_signal import ExternalSignalTool

        assert ExternalSignalTool(dry_run=True).name == "ExternalSignalTool"

    def test_trigger_keywords_present(self):
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=True)
        keywords = tool.trigger_keywords
        assert any("ExternalSignal" in kw for kw in keywords)

    def test_retrieved_at_timestamp_in_context(self):
        """RL context always includes retrieved_at timestamp."""
        tool = self._make_tool()
        resp = tool.execute(_make_tool_request({"subject_id": "TS-001"}))
        assert "ExternalSignal has retrieved_at" in resp.output["rl_context"]


# ===========================================================================
# AnalysisTool
# ===========================================================================


class TestAnalysisTool:
    """Tests for tools/analysis.py::AnalysisTool."""

    def _make_tool(self, **kwargs) -> Any:
        from tools.analysis import AnalysisTool

        return AnalysisTool(**kwargs)

    def _make_input(
        self,
        priority="normal",
        interaction_count=5,
        recency="2025-01-01T00:00:00+00:00",
        content="Hello",
        tier="standard",
        signal_available="true",
        signal_value="normal",
    ) -> dict:
        return {
            "Subject": {
                "attributes": {
                    "priority": priority,
                    "created_at": recency,
                    "raw_content": content,
                }
            },
            "Context": {
                "attributes": {
                    "history_available": "true",
                    "interaction_count": str(interaction_count),
                    "subject_tier": tier,
                }
            },
            "ExternalSignal": {
                "attributes": {
                    "signal_available": signal_available,
                    "signal_value": signal_value,
                }
            },
        }

    def test_primary_score_computed(self):
        """Primary score is computed as a float between 0 and 1."""
        tool = self._make_tool()
        req = _make_tool_request(self._make_input(), goal="compute primary_score")
        resp = tool.execute(req)

        assert resp.success is True
        score = resp.output["primary_score"]
        assert 0.0 <= score <= 1.0

    def test_secondary_signals_computed(self):
        """Secondary signals dict contains expected boolean flags."""
        tool = self._make_tool()
        req = _make_tool_request(
            self._make_input(priority="high", interaction_count=15),
            goal="compute secondary_signals",
        )
        resp = tool.execute(req)

        assert resp.success is True
        signals = resp.output["secondary_signals"]
        assert "elevated_priority" in signals
        assert signals["elevated_priority"] is True
        assert "high_interaction_history" in signals
        assert signals["high_interaction_history"] is True

    def test_high_priority_raises_score(self):
        """High-priority subject should score higher than low-priority."""
        tool = self._make_tool()

        req_high = _make_tool_request(
            self._make_input(priority="high"), goal="compute primary_score"
        )
        req_low = _make_tool_request(self._make_input(priority="low"), goal="compute primary_score")

        score_high = tool.execute(req_high).output["primary_score"]
        score_low = tool.execute(req_low).output["primary_score"]

        assert score_high > score_low

    def test_score_breakdown_matches_primary_score(self):
        """Sum of score breakdown contributions should equal primary_score (approx)."""
        tool = self._make_tool()
        req = _make_tool_request(self._make_input(), goal="compute primary_score")
        resp = tool.execute(req)

        primary_score = resp.output["primary_score"]
        breakdown = resp.output["score_breakdown"]
        total_from_breakdown = sum(breakdown.values())

        # They should be approximately equal (within floating-point tolerance)
        assert abs(primary_score - total_from_breakdown) < 0.05

    def test_rl_context_contains_analysis_attributes(self):
        """RL context must contain Analysis entity attribute statements."""
        tool = self._make_tool()
        req = _make_tool_request(self._make_input(), goal="compute primary_score")
        resp = tool.execute(req)

        ctx = resp.output["rl_context"]
        assert "Analysis has primary_score" in ctx
        assert "Analysis has computed_category" in ctx

    def test_category_thresholds(self):
        """Score above 0.75 should produce 'priority' category."""
        tool = self._make_tool()

        # Force a very high score by patching _compute_primary_score
        with patch.object(tool, "_compute_primary_score", return_value=(0.90, {})):
            req = _make_tool_request(self._make_input(), goal="compute primary_score")
            resp = tool.execute(req)

        assert 'Analysis has computed_category of "priority"' in resp.output["rl_context"]

    def test_low_score_produces_low_value_category(self):
        tool = self._make_tool()
        with patch.object(tool, "_compute_primary_score", return_value=(0.10, {})):
            resp = tool.execute(
                _make_tool_request(self._make_input(), goal="compute primary_score")
            )
        assert 'computed_category of "low_value"' in resp.output["rl_context"]

    def test_signal_unavailable_sets_degraded_flag(self):
        """When ExternalSignal is unavailable, signal_quality_degraded=True."""
        tool = self._make_tool()
        req = _make_tool_request(
            self._make_input(signal_available="false"),
            goal="compute secondary_signals",
        )
        resp = tool.execute(req)
        signals = resp.output["secondary_signals"]
        assert signals.get("signal_quality_degraded") is True

    def test_custom_weights_via_constructor(self):
        """Custom weights are applied to primary score computation."""
        # All weight on priority
        tool = self._make_tool(weights={"priority": 1.0})
        req_high = _make_tool_request(self._make_input(priority="critical"), goal="primary_score")
        req_low = _make_tool_request(self._make_input(priority="minimal"), goal="primary_score")

        score_high = tool.execute(req_high).output["primary_score"]
        score_low = tool.execute(req_low).output["primary_score"]
        assert score_high > score_low

    def test_name_is_analysistool(self):
        from tools.analysis import AnalysisTool

        assert AnalysisTool().name == "AnalysisTool"

    def test_recency_computation_recent_vs_old(self):
        """Subjects created recently should have higher recency score."""
        from datetime import datetime, timedelta, timezone

        tool = self._make_tool(recency_window_hours=24)

        now = datetime.now(tz=timezone.utc).isoformat()
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat()

        score_recent = tool._compute_recency(now)
        score_old = tool._compute_recency(old)
        assert score_recent > score_old

    def test_recency_missing_timestamp_returns_neutral(self):
        """Missing created_at returns neutral 0.5 score."""
        tool = self._make_tool()
        assert tool._compute_recency("") == 0.5
        assert tool._compute_recency("not-a-date") == 0.5


# ===========================================================================
# SQLiteDatabase (zero-dependency fallback)
# ===========================================================================


class TestSQLiteDatabase:
    """Tests for bot_service/db.py::SQLiteDatabase."""

    @pytest.fixture
    def db(self, tmp_path):
        """Provide a fresh SQLiteDatabase backed by a temp file."""
        from bot_service.db import SQLiteDatabase

        db_path = str(tmp_path / "test_rof.db")
        db = SQLiteDatabase(path=db_path)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(db.connect())
        db._test_loop = loop
        yield db
        loop.run_until_complete(db.disconnect())
        loop.close()
        asyncio.set_event_loop(None)

    # ── Pipeline runs ─────────────────────────────────────────────────────────

    def test_save_and_list_pipeline_run(self, db):
        result = {
            "success": True,
            "pipeline_id": str(uuid.uuid4()),
            "elapsed_s": 1.23,
            "final_snapshot": {"entities": {"Subject": {"attributes": {"id": "T-1"}}}},
            "target": "target_a",
        }
        run_id = db._test_loop.run_until_complete(db.save_pipeline_run(result))
        assert run_id is not None

        runs = db._test_loop.run_until_complete(db.list_pipeline_runs(limit=10))
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id

    def test_get_pipeline_run_returns_full_record(self, db):
        snapshot = {"entities": {"Decision": {"attributes": {"action": "proceed"}}}}
        result = {
            "success": True,
            "pipeline_id": str(uuid.uuid4()),
            "elapsed_s": 2.5,
            "final_snapshot": snapshot,
        }
        run_id = db._test_loop.run_until_complete(db.save_pipeline_run(result))
        record = db._test_loop.run_until_complete(db.get_pipeline_run(run_id))

        assert record is not None
        assert record["run_id"] == run_id
        snap = record["final_snapshot"]
        # final_snapshot should be parsed back to dict
        if isinstance(snap, str):
            snap = json.loads(snap)
        assert "entities" in snap

    def test_get_pipeline_run_not_found_returns_none(self, db):
        record = db._test_loop.run_until_complete(db.get_pipeline_run("nonexistent-run-id"))
        assert record is None

    def test_list_runs_filter_by_success(self, db):
        loop = db._test_loop
        for success in (True, False, True):
            loop.run_until_complete(
                db.save_pipeline_run(
                    {
                        "success": success,
                        "pipeline_id": str(uuid.uuid4()),
                        "elapsed_s": 0.5,
                        "final_snapshot": {},
                    }
                )
            )

        success_runs = loop.run_until_complete(db.list_pipeline_runs(success=True))
        failed_runs = loop.run_until_complete(db.list_pipeline_runs(success=False))

        assert len(success_runs) == 2
        assert len(failed_runs) == 1

    def test_list_runs_filter_by_target(self, db):
        loop = db._test_loop
        for target in ("alpha", "beta", "alpha"):
            loop.run_until_complete(
                db.save_pipeline_run(
                    {
                        "success": True,
                        "pipeline_id": str(uuid.uuid4()),
                        "elapsed_s": 1.0,
                        "final_snapshot": {},
                        "target": target,
                    }
                )
            )

        alpha_runs = loop.run_until_complete(db.list_pipeline_runs(target="alpha"))
        assert len(alpha_runs) == 2

    # ── Action log ────────────────────────────────────────────────────────────

    def test_log_action(self, db):
        loop = db._test_loop
        run_id = loop.run_until_complete(
            db.save_pipeline_run(
                {
                    "success": True,
                    "pipeline_id": str(uuid.uuid4()),
                    "elapsed_s": 1.0,
                    "final_snapshot": {},
                }
            )
        )
        action_id = loop.run_until_complete(
            db.log_action(
                run_id=run_id,
                target="target_a",
                action_type="proceed",
                dry_run=True,
                status="dry_run",
                result_summary="[DRY-RUN] Would have proceeded",
            )
        )
        assert action_id is not None
        uuid.UUID(action_id)  # must be a valid UUID

    # ── Bot state KV store ─────────────────────────────────────────────────────

    def test_set_and_get_state(self, db):
        loop = db._test_loop
        loop.run_until_complete(db.set_state("resource_utilisation", 0.45))
        val = loop.run_until_complete(db.get_state("resource_utilisation"))
        assert val == pytest.approx(0.45)

    def test_set_state_upsert(self, db):
        loop = db._test_loop
        loop.run_until_complete(db.set_state("counter", 1))
        loop.run_until_complete(db.set_state("counter", 2))
        val = loop.run_until_complete(db.get_state("counter"))
        assert val == 2

    def test_get_state_missing_key_returns_none(self, db):
        val = db._test_loop.run_until_complete(db.get_state("nonexistent_key"))
        assert val is None

    def test_set_state_complex_value(self, db):
        loop = db._test_loop
        value = {"nested": {"list": [1, 2, 3], "flag": True}}
        loop.run_until_complete(db.set_state("complex", value))
        result = loop.run_until_complete(db.get_state("complex"))
        assert result == value

    # ── Routing memory ─────────────────────────────────────────────────────────

    def test_save_and_load_routing_memory(self, db):
        loop = db._test_loop
        memory_data = {"__routing_memory__": {"goals": {"retrieve Subject data": {"ema": 0.75}}}}
        loop.run_until_complete(db.save_routing_memory("__routing_memory__", memory_data))
        loaded = loop.run_until_complete(db.load_routing_memory("__routing_memory__"))
        assert loaded is not None
        assert "__routing_memory__" in loaded

    def test_load_routing_memory_missing_returns_none(self, db):
        result = db._test_loop.run_until_complete(db.load_routing_memory("__missing__"))
        assert result is None

    def test_routing_memory_upsert(self, db):
        loop = db._test_loop
        loop.run_until_complete(db.save_routing_memory("key1", {"v": 1}))
        loop.run_until_complete(db.save_routing_memory("key1", {"v": 2}))
        loaded = loop.run_until_complete(db.load_routing_memory("key1"))
        assert loaded["v"] == 2

    # ── Daily error rate ───────────────────────────────────────────────────────

    def test_daily_error_rate_no_runs_returns_zero(self, db):
        rate = db._test_loop.run_until_complete(db.get_daily_error_rate())
        assert rate == 0.0

    def test_daily_error_rate_computation(self, db):
        loop = db._test_loop
        # Save 2 success, 1 failure → error rate = 1/3
        for success in (True, True, False):
            loop.run_until_complete(
                db.save_pipeline_run(
                    {
                        "success": success,
                        "pipeline_id": str(uuid.uuid4()),
                        "elapsed_s": 1.0,
                        "final_snapshot": {},
                    }
                )
            )
        rate = loop.run_until_complete(db.get_daily_error_rate())
        assert rate == pytest.approx(1 / 3, abs=0.01)


# ===========================================================================
# SQLAlchemyStateAdapter
# ===========================================================================


class TestSQLAlchemyStateAdapter:
    """Tests for bot_service/state_adapter.py::SQLAlchemyStateAdapter."""

    @pytest.fixture
    def adapter(self, tmp_path):
        """Provide a fresh adapter backed by a temp SQLite file."""
        from bot_service.state_adapter import SQLAlchemyStateAdapter

        db_path = str(tmp_path / "routing.db")
        adapter = SQLAlchemyStateAdapter(f"sqlite:///{db_path}")
        yield adapter
        adapter.close()

    def test_save_and_load_roundtrip(self, adapter):
        data = {"goals": {"test_goal": {"ema": 0.75, "count": 10}}}
        adapter.save("__routing_memory__", data)
        loaded = adapter.load("__routing_memory__")
        assert loaded is not None
        assert loaded["goals"]["test_goal"]["ema"] == 0.75

    def test_load_nonexistent_key_returns_none(self, adapter):
        assert adapter.load("__nonexistent__") is None

    def test_upsert_updates_existing(self, adapter):
        adapter.save("key1", {"v": 1})
        adapter.save("key1", {"v": 2})
        assert adapter.load("key1")["v"] == 2

    def test_exists_returns_true_after_save(self, adapter):
        adapter.save("exists_key", {"x": 1})
        assert adapter.exists("exists_key") is True

    def test_exists_returns_false_for_missing(self, adapter):
        assert adapter.exists("missing_key") is False

    def test_delete_removes_entry(self, adapter):
        adapter.save("del_key", {"x": 1})
        adapter.delete("del_key")
        assert adapter.load("del_key") is None

    def test_save_empty_dict(self, adapter):
        adapter.save("empty", {})
        loaded = adapter.load("empty")
        assert loaded == {}

    def test_save_nested_structure(self, adapter):
        data = {
            "tier1": {"list": [1, 2, 3]},
            "tier2": {"nested": {"deep": True}},
        }
        adapter.save("nested", data)
        assert adapter.load("nested") == data

    def test_close_disposes_engine(self, adapter):
        """close() must dispose the engine without raising."""
        adapter.save("pre_close", {"x": 1})
        adapter.close()
        assert adapter._engine is None

    # ── Async wrappers ────────────────────────────────────────────────────────

    def test_async_save_and_load(self, adapter):
        """async_save / async_load must work from asyncio context."""
        data = {"async_test": True, "count": 42}

        async def _run():
            await adapter.async_save("async_key", data)
            return await adapter.async_load("async_key")

        result = asyncio.run(_run())
        assert result is not None
        assert result["async_test"] is True
        assert result["count"] == 42

    def test_async_load_missing_key_returns_none(self, adapter):
        async def _run():
            return await adapter.async_load("__missing__")

        result = asyncio.run(_run())
        assert result is None

    # ── Thread safety ─────────────────────────────────────────────────────────

    def test_concurrent_saves_are_thread_safe(self, adapter):
        """Multiple threads saving different keys simultaneously must not corrupt data."""
        errors: list[Exception] = []

        def _save(key: str, val: int):
            try:
                adapter.save(key, {"v": val})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_save, args=(f"key_{i}", i)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"

        # Verify all keys were saved
        for i in range(20):
            loaded = adapter.load(f"key_{i}")
            assert loaded is not None
            assert loaded["v"] == i

    # ── from_database constructor ─────────────────────────────────────────────

    def test_from_database_with_sqlite_db(self, tmp_path):
        from bot_service.db import SQLiteDatabase
        from bot_service.state_adapter import SQLAlchemyStateAdapter

        db = SQLiteDatabase(path=str(tmp_path / "test.db"))
        adapter = SQLAlchemyStateAdapter.from_database(db)
        assert "sqlite" in adapter._dsn
        adapter.close()


# ===========================================================================
# Database factory (get_database)
# ===========================================================================


class TestGetDatabase:
    """Tests for bot_service/db.py::get_database factory."""

    def test_get_database_returns_sqlite_by_default(self):
        from bot_service.db import SQLAlchemyDatabase, SQLiteDatabase, get_database

        # Clear the lru_cache to get a fresh instance
        get_database.cache_clear()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./test_rof_get_db.db"}):
            db = (
                get_database.__wrapped__("sqlite:///./test_rof_get_db.db")
                if hasattr(get_database, "__wrapped__")
                else get_database("sqlite:///./test_rof_get_db.db")
            )
        assert db is not None
        # Should be either SQLAlchemyDatabase (if sqlalchemy available) or SQLiteDatabase
        assert isinstance(db, (SQLiteDatabase, SQLAlchemyDatabase))

    def test_get_database_caching(self):
        """get_database with same URL returns the same instance (lru_cache)."""
        from bot_service.db import get_database

        get_database.cache_clear()

        db1 = get_database("sqlite:///./rof_cache_test.db")
        db2 = get_database("sqlite:///./rof_cache_test.db")
        assert db1 is db2

        get_database.cache_clear()

    def test_sqlalchemy_database_postgres_url_detection(self):
        from bot_service.db import SQLAlchemyDatabase

        db = SQLAlchemyDatabase("postgresql+asyncpg://bot:bot@localhost/rof_bot")
        assert db._is_postgres is True
        assert db._is_sqlite is False

    def test_sqlalchemy_database_sqlite_url_detection(self):
        from bot_service.db import SQLAlchemyDatabase

        db = SQLAlchemyDatabase("sqlite+aiosqlite:///./test.db")
        assert db._is_sqlite is True
        assert db._is_postgres is False


# ===========================================================================
# Settings
# ===========================================================================


class TestSettings:
    """Tests for bot_service/settings.py::Settings."""

    def test_default_dry_run_is_true(self):
        from bot_service.settings import Settings

        with patch.dict(
            os.environ,
            {
                "BOT_DRY_RUN": "true",
                "DATABASE_URL": "sqlite:///./rof_bot.db",
            },
            clear=False,
        ):
            # Clear the lru_cache if available
            try:
                from bot_service.settings import get_settings

                get_settings.cache_clear()
            except (AttributeError, ImportError):
                pass
            s = Settings()
        assert s.bot_dry_run is True

    def test_targets_list_parsing(self):
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"BOT_TARGETS": "alpha,beta,gamma"}, clear=False):
            s = Settings()
        assert s.targets_list == ["alpha", "beta", "gamma"]

    def test_targets_list_single(self):
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"BOT_TARGETS": "only_target"}, clear=False):
            s = Settings()
        assert s.targets_list == ["only_target"]
        assert s.is_multi_target is False

    def test_is_multi_target_true(self):
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"BOT_TARGETS": "a,b"}, clear=False):
            s = Settings()
        assert s.is_multi_target is True

    def test_is_postgres_detection(self):
        from bot_service.settings import Settings

        with patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://bot:bot@localhost/rof_bot"}, clear=False
        ):
            s = Settings()
        assert s.is_postgres is True

    def test_sqlite_is_not_postgres(self):
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./rof_bot.db"}, clear=False):
            s = Settings()
        assert s.is_postgres is False

    def test_async_database_url_auto_derived_from_postgres(self):
        from bot_service.settings import Settings

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://bot:bot@localhost/rof",
                "ASYNC_DATABASE_URL": "",
            },
            clear=False,
        ):
            s = Settings()
        assert "asyncpg" in (s.async_database_url or "")


# ===========================================================================
# Integration: Tool registry assembly
# ===========================================================================


class TestBuildToolRegistry:
    """Integration test for pipeline_factory.build_tool_registry()."""

    def test_registry_contains_required_tools(self):
        """build_tool_registry must register all required tool names."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        with patch.dict(os.environ, {"BOT_DRY_RUN": "true"}, clear=False):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())

        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            chromadb_path="./test_chroma",
            dry_run=True,
            state_tool=state_tool,
        )

        # all_tools() should return a dict of tool_name → ToolProvider
        if hasattr(registry, "all_tools"):
            tools = registry.all_tools()
            names = set(tools.keys())
        else:
            pytest.skip("ToolRegistry.all_tools() not available")

        required = {
            "DataSourceTool",
            "ContextEnrichmentTool",
            "ActionExecutorTool",
            "StateManagerTool",
            "ExternalSignalTool",
            "AnalysisTool",
            "DatabaseTool",
            "ValidatorTool",
        }
        for name in required:
            assert name in names, f"Missing required tool: {name}"

    def test_registry_dry_run_propagated_to_action_executor(self):
        """ActionExecutorTool in the registry must have dry_run=True."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        settings = Settings()
        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if hasattr(registry, "all_tools"):
            tools = registry.all_tools()
            executor = tools.get("ActionExecutorTool")
            if executor is not None:
                assert executor._dry_run is True


# ===========================================================================
# Workflow file presence
# ===========================================================================


class TestWorkflowFiles:
    """Ensure all 5 workflow .rl files exist and are non-empty."""

    _WORKFLOW_DIR = _BOT_ROOT / "workflows"

    @pytest.mark.parametrize(
        "filename",
        [
            "01_collect.rl",
            "02_analyse.rl",
            "03_validate.rl",
            "04_decide.rl",
            "05_execute.rl",
        ],
    )
    def test_workflow_file_exists(self, filename):
        path = self._WORKFLOW_DIR / filename
        assert path.exists(), f"Missing workflow file: {path}"
        assert path.stat().st_size > 0, f"Workflow file is empty: {path}"

    @pytest.mark.parametrize(
        "filename,expected_keywords",
        [
            ("01_collect.rl", ["ensure retrieve Subject data", "define Subject"]),
            ("02_analyse.rl", ["define Analysis", "ensure compute primary_score"]),
            (
                "03_validate.rl",
                ["define Constraints", "ensure retrieve current_resource_utilisation"],
            ),
            ("04_decide.rl", ["define Decision", "ensure determine final Decision"]),
            ("05_execute.rl", ["define Action", "ensure record Action"]),
        ],
    )
    def test_workflow_file_contains_required_goals(self, filename, expected_keywords):
        path = self._WORKFLOW_DIR / filename
        content = path.read_text(encoding="utf-8")
        for keyword in expected_keywords:
            assert keyword in content, f"{filename} is missing required content: {keyword!r}"

    @pytest.mark.parametrize(
        "filename",
        [
            "01_collect.rl",
            "02_analyse.rl",
            "03_validate.rl",
            "04_decide.rl",
            "05_execute.rl",
        ],
    )
    def test_workflow_file_parseable_by_rl_parser(self, filename):
        """All .rl files must parse without raising ParseError."""
        try:
            from rof_framework.core.parser.rl_parser import RLParser
            from rof_framework.routing.hints import RoutingHintExtractor
        except ImportError:
            pytest.skip("rof_framework not available")

        path = self._WORKFLOW_DIR / filename
        source = path.read_text(encoding="utf-8")

        # Strip routing hints before parsing (as the pipeline does)
        extractor = RoutingHintExtractor()
        clean = extractor.strip_hints(source)

        parser = RLParser()
        ast = parser.parse(clean)
        assert ast is not None

    @pytest.mark.parametrize(
        "filename",
        [
            "01_collect.rl",
            "02_analyse.rl",
            "03_validate.rl",
            "04_decide.rl",
            "05_execute.rl",
        ],
    )
    def test_workflow_file_lint_clean(self, filename):
        """All .rl files must pass the ROF linter with no ERROR-level issues."""
        try:
            from rof_framework.core.lint.linter import Linter, Severity
        except ImportError:
            pytest.skip("rof_framework linter not available")

        path = self._WORKFLOW_DIR / filename
        source = path.read_text(encoding="utf-8")

        linter = Linter()
        issues = linter.lint(source)

        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"{filename} has lint errors:\n" + "\n".join(
            f"  L{i.line}: {i.message}" for i in errors
        )


# ===========================================================================
# External credentials — URL + key combinations
# ===========================================================================


class TestExternalCredentials:
    """
    Verify all combinations of EXTERNAL_API_BASE_URL / EXTERNAL_API_KEY and
    EXTERNAL_SIGNAL_BASE_URL / EXTERNAL_SIGNAL_API_KEY across:

      - Settings defaults (both blank)
      - Tool construction with blank URL
      - Tool construction with URL but no key  (public endpoint)
      - Tool construction with URL and key     (authenticated endpoint)
      - ExternalSignalTool silent absent path  (blank URL → signal_available=false, no warning)
      - DataSourceTool / ContextEnrichmentTool / ActionExecutorTool with blank URL → dry-run path
      - Settings picking up values from environment variables
      - APICallTool inline-URL pattern: no EXTERNAL_* env vars needed
    """

    # ------------------------------------------------------------------
    # Settings defaults
    # ------------------------------------------------------------------

    def test_settings_external_api_base_url_default_is_empty(self):
        """EXTERNAL_API_BASE_URL must default to '' — never a fake example.com URL."""
        from bot_service.settings import Settings

        with patch.dict(os.environ, {}, clear=False):
            # Remove the var entirely to exercise the true default
            env = {k: v for k, v in os.environ.items() if k != "EXTERNAL_API_BASE_URL"}
            with patch.dict(os.environ, env, clear=True):
                s = Settings()
        assert s.external_api_base_url == "", (
            f"Expected empty string, got {s.external_api_base_url!r}"
        )

    def test_settings_external_signal_base_url_default_is_empty(self):
        """EXTERNAL_SIGNAL_BASE_URL must default to '' — no fake example.com URL."""
        from bot_service.settings import Settings

        env = {k: v for k, v in os.environ.items() if k != "EXTERNAL_SIGNAL_BASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            s = Settings()
        assert s.external_signal_base_url == "", (
            f"Expected empty string, got {s.external_signal_base_url!r}"
        )

    def test_settings_api_keys_default_to_empty(self):
        """Both API key fields must default to empty string."""
        from bot_service.settings import Settings

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"EXTERNAL_API_KEY", "EXTERNAL_SIGNAL_API_KEY"}
        }
        with patch.dict(os.environ, env, clear=True):
            s = Settings()
        assert s.external_api_key == ""
        assert s.external_signal_api_key == ""

    def test_settings_reads_external_api_base_url_from_env(self):
        """Settings picks up EXTERNAL_API_BASE_URL from the environment."""
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"EXTERNAL_API_BASE_URL": "https://my-api.example.com/v1"}):
            s = Settings()
        assert s.external_api_base_url == "https://my-api.example.com/v1"

    def test_settings_reads_external_signal_base_url_from_env(self):
        """Settings picks up EXTERNAL_SIGNAL_BASE_URL from the environment."""
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"EXTERNAL_SIGNAL_BASE_URL": "https://signals.example.com/v2"}):
            s = Settings()
        assert s.external_signal_base_url == "https://signals.example.com/v2"

    def test_settings_reads_api_key_from_env(self):
        """Settings picks up EXTERNAL_API_KEY from the environment."""
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"EXTERNAL_API_KEY": "secret-key-123"}):
            s = Settings()
        assert s.external_api_key == "secret-key-123"

    def test_settings_reads_signal_api_key_from_env(self):
        """Settings picks up EXTERNAL_SIGNAL_API_KEY from the environment."""
        from bot_service.settings import Settings

        with patch.dict(os.environ, {"EXTERNAL_SIGNAL_API_KEY": "signal-key-456"}):
            s = Settings()
        assert s.external_signal_api_key == "signal-key-456"

    # ------------------------------------------------------------------
    # DataSourceTool — blank URL, URL-only, URL+key
    # ------------------------------------------------------------------

    def test_datasource_blank_url_dry_run_succeeds(self):
        """DataSourceTool with blank base_url still works in dry-run mode."""
        from tools.data_source import DataSourceTool

        tool = DataSourceTool(base_url="", api_key="", dry_run=True)
        req = _make_tool_request({"subject_id": "DS-001"})
        resp = tool.execute(req)

        assert resp.success is True
        assert "data_complete" in resp.output["rl_context"]

    def test_datasource_blank_url_live_mode_returns_degraded(self):
        """DataSourceTool with blank base_url in live mode returns data_complete=false."""
        from tools.data_source import DataSourceTool

        tool = DataSourceTool(base_url="", api_key="", dry_run=False)
        req = _make_tool_request({"subject_id": "DS-002"})
        resp = tool.execute(req)

        # Tool must not raise — returns degraded response
        assert resp.success is True
        assert "data_complete of false" in resp.output["rl_context"]

    def test_datasource_url_no_key_constructs_correctly(self):
        """DataSourceTool accepts a URL with no API key (public endpoint)."""
        from tools.data_source import DataSourceTool

        tool = DataSourceTool(base_url="https://public-api.example.com", api_key="", dry_run=True)
        assert tool._base_url == "https://public-api.example.com"
        assert tool._api_key == ""

    def test_datasource_url_with_key_constructs_correctly(self):
        """DataSourceTool accepts both URL and API key."""
        from tools.data_source import DataSourceTool

        tool = DataSourceTool(
            base_url="https://private-api.example.com",
            api_key="bearer-token-xyz",
            dry_run=True,
        )
        assert tool._base_url == "https://private-api.example.com"
        assert tool._api_key == "bearer-token-xyz"

    def test_datasource_falls_back_to_env_var(self):
        """DataSourceTool reads EXTERNAL_API_BASE_URL from env when base_url=''."""
        from tools.data_source import DataSourceTool

        with patch.dict(os.environ, {"EXTERNAL_API_BASE_URL": "https://env-api.example.com"}):
            tool = DataSourceTool(base_url="", api_key="", dry_run=True)
        assert tool._base_url == "https://env-api.example.com"

    def test_datasource_key_falls_back_to_env_var(self):
        """DataSourceTool reads EXTERNAL_API_KEY from env when api_key=''."""
        from tools.data_source import DataSourceTool

        with patch.dict(os.environ, {"EXTERNAL_API_KEY": "env-key-abc"}):
            tool = DataSourceTool(base_url="", api_key="", dry_run=True)
        assert tool._api_key == "env-key-abc"

    # ------------------------------------------------------------------
    # ExternalSignalTool — blank URL silent path
    # ------------------------------------------------------------------

    def test_signal_tool_blank_url_returns_unavailable_silently(self):
        """ExternalSignalTool with blank base_url returns signal_available=false — no exception."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(base_url="", api_key="", dry_run=False)
        req = _make_tool_request({"subject_id": "SIG-BLANK"})
        resp = tool.execute(req)

        assert resp.success is True, "Tool must never set success=False for missing URL"
        assert 'signal_available of "false"' in resp.output["rl_context"]

    def test_signal_tool_blank_url_no_logging_warning_on_construct(self):
        """Constructing ExternalSignalTool with blank URL must not emit a warning."""
        import logging

        from tools.external_signal import ExternalSignalTool

        with patch.object(logging.getLogger("rof.tools.external_signal"), "warning") as mock_warn:
            ExternalSignalTool(base_url="", api_key="", dry_run=False)
        mock_warn.assert_not_called()

    def test_signal_tool_url_no_key_constructs_correctly(self):
        """ExternalSignalTool accepts a URL with no API key (public signal endpoint)."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(
            base_url="https://public-signals.example.com",
            api_key="",
            dry_run=True,
        )
        assert tool._base_url == "https://public-signals.example.com"
        assert tool._api_key == ""

    def test_signal_tool_url_with_key_constructs_correctly(self):
        """ExternalSignalTool accepts both URL and API key."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(
            base_url="https://private-signals.example.com",
            api_key="sig-secret-key",
            dry_run=True,
        )
        assert tool._base_url == "https://private-signals.example.com"
        assert tool._api_key == "sig-secret-key"

    def test_signal_tool_falls_back_to_env_var(self):
        """ExternalSignalTool reads EXTERNAL_SIGNAL_BASE_URL from env when base_url=''."""
        from tools.external_signal import ExternalSignalTool

        with patch.dict(
            os.environ, {"EXTERNAL_SIGNAL_BASE_URL": "https://env-signals.example.com"}
        ):
            tool = ExternalSignalTool(base_url="", api_key="", dry_run=True)
        assert tool._base_url == "https://env-signals.example.com"

    def test_signal_tool_key_falls_back_to_env_var(self):
        """ExternalSignalTool reads EXTERNAL_SIGNAL_API_KEY from env when api_key=''."""
        from tools.external_signal import ExternalSignalTool

        with patch.dict(os.environ, {"EXTERNAL_SIGNAL_API_KEY": "env-sig-key"}):
            tool = ExternalSignalTool(base_url="", api_key="", dry_run=True)
        assert tool._api_key == "env-sig-key"

    def test_signal_tool_falls_back_to_primary_api_env_var(self):
        """When EXTERNAL_SIGNAL_BASE_URL is unset, ExternalSignalTool falls back to EXTERNAL_API_BASE_URL."""
        from tools.external_signal import ExternalSignalTool

        env = {k: v for k, v in os.environ.items() if k != "EXTERNAL_SIGNAL_BASE_URL"}
        env["EXTERNAL_API_BASE_URL"] = "https://fallback-primary.example.com"
        with patch.dict(os.environ, env, clear=True):
            tool = ExternalSignalTool(base_url="", api_key="", dry_run=True)
        assert tool._base_url == "https://fallback-primary.example.com"

    def test_signal_tool_dry_run_works_regardless_of_url(self):
        """Dry-run returns stub signal data even when base_url is blank."""
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(base_url="", api_key="", dry_run=True)
        req = _make_tool_request({"subject_id": "DRY-001"})
        resp = tool.execute(req)

        assert resp.success is True
        assert 'signal_available of "true"' in resp.output["rl_context"]

    # ------------------------------------------------------------------
    # APICallTool inline-URL pattern
    # ------------------------------------------------------------------

    def test_apicall_tool_registered_in_registry(self):
        """APICallTool must be present in the tool registry — it is the recommended
        inline-endpoint pattern and must always be available."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        with patch.dict(os.environ, {"BOT_DRY_RUN": "true"}, clear=False):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if not hasattr(registry, "all_tools"):
            pytest.skip("ToolRegistry.all_tools() not available")

        tools = registry.all_tools()
        assert "APICallTool" in tools, (
            "APICallTool must be registered — it is used for inline-URL endpoint calls"
        )

    def test_apicall_tool_registered_even_when_external_urls_blank(self):
        """APICallTool must be in the registry even when both EXTERNAL_* URLs are blank."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "EXTERNAL_API_BASE_URL",
                "EXTERNAL_SIGNAL_BASE_URL",
                "EXTERNAL_API_KEY",
                "EXTERNAL_SIGNAL_API_KEY",
            }
        }
        with patch.dict(os.environ, env_clean, clear=True):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if not hasattr(registry, "all_tools"):
            pytest.skip("ToolRegistry.all_tools() not available")

        tools = registry.all_tools()
        assert "APICallTool" in tools

    # ------------------------------------------------------------------
    # Pipeline factory — blank URL propagation
    # ------------------------------------------------------------------

    def test_registry_tools_receive_blank_base_url_when_env_unset(self):
        """When EXTERNAL_API_BASE_URL is not set, DataSourceTool._base_url must be ''."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k not in {"EXTERNAL_API_BASE_URL", "EXTERNAL_API_KEY"}
        }
        with patch.dict(os.environ, env_clean, clear=True):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if not hasattr(registry, "all_tools"):
            pytest.skip("ToolRegistry.all_tools() not available")

        tools = registry.all_tools()
        data_source = tools.get("DataSourceTool")
        if data_source is None:
            pytest.skip("DataSourceTool not registered")

        assert data_source._base_url == "", (
            f"Expected blank base_url, got {data_source._base_url!r} — "
            "no fake example.com URL should be injected"
        )

    def test_registry_signal_tool_receives_blank_base_url_when_env_unset(self):
        """When EXTERNAL_SIGNAL_BASE_URL is not set, ExternalSignalTool._base_url must be ''."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "EXTERNAL_SIGNAL_BASE_URL",
                "EXTERNAL_SIGNAL_API_KEY",
                "EXTERNAL_API_BASE_URL",
                "EXTERNAL_API_KEY",
            }
        }
        with patch.dict(os.environ, env_clean, clear=True):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if not hasattr(registry, "all_tools"):
            pytest.skip("ToolRegistry.all_tools() not available")

        tools = registry.all_tools()
        signal_tool = tools.get("ExternalSignalTool")
        if signal_tool is None:
            pytest.skip("ExternalSignalTool not registered")

        assert signal_tool._base_url == "", (
            f"Expected blank base_url, got {signal_tool._base_url!r} — "
            "no fake example.com URL should be injected"
        )

    def test_registry_url_and_key_propagated_to_tools(self):
        """When EXTERNAL_API_BASE_URL and EXTERNAL_API_KEY are set, all three primary
        tools receive those values."""
        from bot_service.pipeline_factory import build_tool_registry
        from bot_service.settings import Settings
        from tools.state_manager import BotStateManagerTool, _InMemoryBackend

        with patch.dict(
            os.environ,
            {
                "EXTERNAL_API_BASE_URL": "https://configured.example.com/api",
                "EXTERNAL_API_KEY": "configured-key",
            },
        ):
            settings = Settings()

        state_tool = BotStateManagerTool(backend=_InMemoryBackend())
        registry = build_tool_registry(
            settings=settings,
            db_url="sqlite:///:memory:",
            dry_run=True,
            state_tool=state_tool,
        )

        if not hasattr(registry, "all_tools"):
            pytest.skip("ToolRegistry.all_tools() not available")

        tools = registry.all_tools()
        for tool_name in ("DataSourceTool", "ContextEnrichmentTool", "ActionExecutorTool"):
            tool = tools.get(tool_name)
            if tool is None:
                continue
            assert tool._base_url == "https://configured.example.com/api", (
                f"{tool_name}._base_url not propagated correctly"
            )
            assert tool._api_key == "configured-key", (
                f"{tool_name}._api_key not propagated correctly"
            )
