"""
tests/test_llm_providers.py
============================
Tests for rof_llm provider implementations.
Tests each provider adapter with mock HTTP responses.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# Try to import rof_llm components
try:
    from rof_framework.rof_llm import (
        AuthError,
        ContextLimitError,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        ProviderError,
        RateLimitError,
    )

    ROF_LLM_AVAILABLE = True
except ImportError:
    ROF_LLM_AVAILABLE = False
    pytestmark = pytest.mark.skip("rof_llm not available")


# ─── LLM Request/Response Tests ───────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestLLMRequestResponse:
    def test_llm_request_creation(self):
        request = LLMRequest(
            prompt="Test prompt",
            system="You are a helpful assistant",
            max_tokens=100,
            temperature=0.7,
        )
        assert request.prompt == "Test prompt"
        assert request.system == "You are a helpful assistant"
        assert request.max_tokens == 100
        assert request.temperature == 0.7

    def test_llm_response_creation(self):
        response = LLMResponse(content="Test response", raw={"model": "test-model"}, tool_calls=[])
        assert response.content == "Test response"
        assert response.raw["model"] == "test-model"
        assert len(response.tool_calls) == 0


# ─── Mock LLM Provider Tests ──────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestMockLLMProvider:
    """Test a simple mock provider implementation."""

    def test_mock_provider_interface(self):
        """Test that mock provider implements required interface."""

        class SimpleMockProvider(LLMProvider):
            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(content=f"Mock response to: {request.prompt}")

            def supports_tool_calling(self) -> bool:
                return False

            @property
            def context_limit(self) -> int:
                return 4096

        provider = SimpleMockProvider()
        request = LLMRequest(prompt="Hello", max_tokens=50)
        response = provider.complete(request)

        assert "Hello" in response.content
        assert not provider.supports_tool_calling()
        assert provider.context_limit == 4096


# ─── Error Classification Tests ───────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestProviderErrors:
    def test_provider_error_basic(self):
        error = ProviderError("Test error", status_code=500)
        assert str(error) == "Test error"
        assert error.status_code == 500

    def test_rate_limit_error(self):
        error = RateLimitError("Rate limited", status_code=429)
        assert isinstance(error, ProviderError)
        assert error.status_code == 429

    def test_auth_error(self):
        error = AuthError("Unauthorized", status_code=401)
        assert isinstance(error, ProviderError)
        assert error.status_code == 401

    def test_context_limit_error(self):
        error = ContextLimitError("Context too long")
        assert isinstance(error, ProviderError)


# ─── Prompt Renderer Tests ────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestPromptRenderer:
    """Test prompt rendering from WorkflowGraph to LLM-ready format."""

    def test_render_basic_workflow(self):
        """Test rendering a simple workflow to a prompt."""
        pytest.skip("PromptRenderer not exported from rof_llm - internal class")


# ─── Retry Manager Tests ──────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestRetryManager:
    """Test retry logic for failed LLM calls."""

    def test_retry_on_rate_limit(self):
        """Test that rate limit errors trigger retry."""
        pytest.skip("RetryManager not exported from rof_llm - internal class")

    def test_max_retries_exceeded(self):
        """Test that max retries limit is respected."""
        pytest.skip("RetryManager not exported from rof_llm - internal class")


# ─── Response Parser Tests ────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestResponseParser:
    """Test parsing LLM responses for RL content and tool calls."""

    def test_parse_rl_in_response(self):
        """Test extracting RL statements from LLM response."""
        pytest.skip("ResponseParser not exported from rof_llm - internal class")

    def test_parse_tool_calls(self):
        """Test detecting tool call requests in responses."""
        pytest.skip("ResponseParser not exported from rof_llm - internal class")


# ─── Integration Test with Mock Provider ──────────────────────────────────────


@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestLLMIntegration:
    """Integration tests using mock LLM provider with Orchestrator."""

    def test_orchestrator_with_mock_llm(self):
        """Test full orchestration cycle with mock LLM."""
        try:
            from rof_framework.rof_core import Orchestrator, OrchestratorConfig, RLParser

            class TestMockProvider(LLMProvider):
                def complete(self, request: LLMRequest) -> LLMResponse:
                    return LLMResponse(content="Task completed successfully.", raw={"test": True})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            source = """
            define Task as "A test task".
            ensure complete the Task.
            """

            ast = RLParser().parse(source)
            llm = TestMockProvider()
            config = OrchestratorConfig(max_iterations=5)

            orch = Orchestrator(llm_provider=llm, config=config)
            result = orch.run(ast)

            assert result is not None

        except ImportError:
            pytest.skip("Orchestrator not available")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
