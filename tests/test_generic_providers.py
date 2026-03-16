"""
tests/test_generic_providers.py
================================
Tests for the ``rof_providers.PROVIDER_REGISTRY`` contract and the generic
provider discovery mechanism used by the CLI, rof_bot, and the rof_ai_demo.

What is tested
--------------
1. Registry structure contract
   - ``PROVIDER_REGISTRY`` is exported from ``rof_providers``
   - Every entry has the required fields (``cls``, ``label``, ``description``,
     ``api_key_kwarg``, ``env_key``, ``env_fallback``)
   - Every ``cls`` is a concrete subclass of ``LLMProvider``
   - Every ``cls`` implements the full ``LLMProvider`` interface
     (``complete``, ``supports_tool_calling``, ``supports_structured_output``,
     ``context_limit``)

2. CLI discovery helper (``rof_framework.cli.main._load_generic_providers``)
   - Returns a subset of ``PROVIDER_REGISTRY``
   - Every returned entry has a non-None ``cls``
   - Returns ``{}`` gracefully when ``rof_providers`` is not available
     (tested via monkeypatching ``sys.modules``)

3. Pipeline-factory discovery helper
   (``bot_service.pipeline_factory._load_generic_providers``)
   - Same contract as the CLI helper

4. Mock-HTTP ``complete()`` round-trip for every registered provider
   - The provider's ``complete()`` method returns an ``LLMResponse``
   - ``content`` is a string (may be empty)
   - ``tool_calls`` is a list
   - ``raw`` is populated

5. Live smoke test (skipped unless ``ROF_TEST_PROVIDER`` matches a registry key)
   - Uses the shared ``live_llm`` fixture from ``conftest.py``
   - Sends a minimal ``LLMRequest`` and asserts a non-empty ``LLMResponse``

No provider-specific names, class names, endpoint URLs, or environment
variable names appear anywhere in this file.  Everything is discovered
dynamically from ``rof_providers.PROVIDER_REGISTRY``.

Run (unit tests only — no real LLM):
    pytest tests/test_generic_providers.py -v

Run (including live smoke test):
    ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<key> \\
        pytest tests/test_generic_providers.py -v -m live_integration
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse

    ROF_CORE_AVAILABLE = True
except ImportError:
    ROF_CORE_AVAILABLE = False

try:
    import rof_providers

    _REGISTRY: dict[str, dict[str, Any]] = getattr(rof_providers, "PROVIDER_REGISTRY", {})
    ROF_PROVIDERS_AVAILABLE = bool(_REGISTRY)
except ImportError:
    _REGISTRY = {}
    ROF_PROVIDERS_AVAILABLE = False

# Skip the entire module when neither package is present — nothing to test.
pytestmark = pytest.mark.skipif(
    not ROF_CORE_AVAILABLE,
    reason="rof_framework not importable — skipping generic provider tests",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_SPEC_KEYS = ("cls", "label", "description", "api_key_kwarg", "env_key", "env_fallback")

_MINIMAL_OPENAI_RESPONSE: dict = {
    "choices": [
        {
            "finish_reason": "stop",
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello from the mock provider.",
            },
        }
    ],
    "id": "chatcmpl-test-000",
    "model": "test-model",
    "object": "chat.completion",
    "usage": {
        "completion_tokens": 5,
        "prompt_tokens": 3,
        "total_tokens": 8,
    },
}


def _make_mock_http_response(status_code: int = 200, body: dict | None = None) -> MagicMock:
    """Return a mock httpx.Response-like object with a successful JSON body."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    payload = body if body is not None else _MINIMAL_OPENAI_RESPONSE
    mock_resp.json.return_value = payload
    import json

    mock_resp.text = json.dumps(payload)
    return mock_resp


def _dummy_key_for(spec: dict[str, Any]) -> dict[str, Any]:
    """Return constructor kwargs that satisfy the API-key requirement.

    Uses a dummy string value so the constructor does not raise ``AuthError``
    during unit tests where no real credentials are available.
    """
    kwargs: dict[str, Any] = {}
    kwarg_name: str | None = spec.get("api_key_kwarg")
    if kwarg_name:
        kwargs[kwarg_name] = "dummy-test-key-000000"
    return kwargs


# ---------------------------------------------------------------------------
# 1. Registry structure contract
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
class TestRegistryStructure:
    """Verify that every entry in PROVIDER_REGISTRY conforms to the expected schema."""

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_entry_has_all_required_keys(self, name: str) -> None:
        spec = _REGISTRY[name]
        missing = [k for k in _REQUIRED_SPEC_KEYS if k not in spec]
        assert not missing, f"Registry entry '{name}' is missing required keys: {missing}"

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_is_not_none(self, name: str) -> None:
        assert _REGISTRY[name]["cls"] is not None, f"Registry entry '{name}' has cls=None"

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_is_llm_provider_subclass(self, name: str) -> None:
        cls = _REGISTRY[name]["cls"]
        assert issubclass(cls, LLMProvider), (
            f"Registry entry '{name}': {cls.__name__} is not a subclass of LLMProvider"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_implements_complete(self, name: str) -> None:
        cls = _REGISTRY[name]["cls"]
        assert callable(getattr(cls, "complete", None)), (
            f"Registry entry '{name}': {cls.__name__} does not implement complete()"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_implements_supports_tool_calling(self, name: str) -> None:
        cls = _REGISTRY[name]["cls"]
        assert callable(getattr(cls, "supports_tool_calling", None)), (
            f"Registry entry '{name}': {cls.__name__} does not implement supports_tool_calling()"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_implements_supports_structured_output(self, name: str) -> None:
        cls = _REGISTRY[name]["cls"]
        assert callable(getattr(cls, "supports_structured_output", None)), (
            f"Registry entry '{name}': {cls.__name__} "
            "does not implement supports_structured_output()"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_cls_has_context_limit(self, name: str) -> None:
        cls = _REGISTRY[name]["cls"]
        assert hasattr(cls, "context_limit"), (
            f"Registry entry '{name}': {cls.__name__} does not expose context_limit"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_label_is_non_empty_string(self, name: str) -> None:
        label = _REGISTRY[name].get("label", "")
        assert isinstance(label, str) and label.strip(), (
            f"Registry entry '{name}': 'label' must be a non-empty string, got {label!r}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_description_is_non_empty_string(self, name: str) -> None:
        description = _REGISTRY[name].get("description", "")
        assert isinstance(description, str) and description.strip(), (
            f"Registry entry '{name}': 'description' must be a non-empty string, got {description!r}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_env_fallback_is_list(self, name: str) -> None:
        env_fallback = _REGISTRY[name].get("env_fallback", [])
        assert isinstance(env_fallback, list), (
            f"Registry entry '{name}': 'env_fallback' must be a list, got {type(env_fallback)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_api_key_kwarg_is_string_or_none(self, name: str) -> None:
        kwarg = _REGISTRY[name].get("api_key_kwarg")
        assert kwarg is None or isinstance(kwarg, str), (
            f"Registry entry '{name}': 'api_key_kwarg' must be str or None, got {type(kwarg)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_env_key_is_string_or_none(self, name: str) -> None:
        env_key = _REGISTRY[name].get("env_key")
        assert env_key is None or isinstance(env_key, str), (
            f"Registry entry '{name}': 'env_key' must be str or None, got {type(env_key)}"
        )

    def test_registry_is_exported_from_package(self) -> None:
        """PROVIDER_REGISTRY must be importable directly from rof_providers."""
        from rof_providers import PROVIDER_REGISTRY  # noqa: F401

        assert isinstance(PROVIDER_REGISTRY, dict)
        assert len(PROVIDER_REGISTRY) >= 1, "PROVIDER_REGISTRY must contain at least one entry"

    def test_registry_keys_are_lowercase(self) -> None:
        """Registry keys are used as CLI --provider values and must be lowercase."""
        for name in _REGISTRY:
            assert name == name.lower(), (
                f"Registry key '{name}' must be lowercase (it is used as a CLI --provider value)"
            )

    def test_no_duplicate_class_registrations(self) -> None:
        """Each provider class should appear at most once in the registry."""
        seen: dict[type, str] = {}
        for name, spec in _REGISTRY.items():
            cls = spec["cls"]
            if cls in seen:
                pytest.fail(
                    f"Provider class {cls.__name__} is registered under both "
                    f"'{seen[cls]}' and '{name}'"
                )
            seen[cls] = name


# ---------------------------------------------------------------------------
# 2. CLI discovery helper
# ---------------------------------------------------------------------------


class TestCLIDiscoveryHelper:
    """Tests for ``rof_framework.cli.main._load_generic_providers``."""

    def _import_helper(self):
        try:
            from rof_framework.cli.main import _load_generic_providers
        except ImportError:
            pytest.skip("rof_framework.cli.main not importable")
        return _load_generic_providers

    def test_returns_dict(self) -> None:
        fn = self._import_helper()
        result = fn()
        assert isinstance(result, dict)

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_returns_non_empty_when_package_installed(self) -> None:
        fn = self._import_helper()
        result = fn()
        assert len(result) > 0, (
            "_load_generic_providers() returned {} even though rof_providers is installed"
        )

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_all_entries_have_cls(self) -> None:
        fn = self._import_helper()
        for name, spec in fn().items():
            assert spec.get("cls") is not None, (
                f"_load_generic_providers() returned an entry '{name}' with cls=None"
            )

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_all_cls_are_llm_providers(self) -> None:
        fn = self._import_helper()
        for name, spec in fn().items():
            cls = spec["cls"]
            assert issubclass(cls, LLMProvider), (
                f"CLI helper returned '{name}' whose class is not an LLMProvider subclass"
            )

    def test_returns_empty_when_rof_providers_missing(self, monkeypatch) -> None:
        """Simulate rof_providers not being installed."""
        fn = self._import_helper()
        # Temporarily hide rof_providers from the import system
        saved = sys.modules.pop("rof_providers", None)
        try:
            monkeypatch.setitem(sys.modules, "rof_providers", None)  # type: ignore[arg-type]
            result = fn()
            assert result == {}, (
                "_load_generic_providers() must return {} when rof_providers is not available"
            )
        finally:
            if saved is not None:
                sys.modules["rof_providers"] = saved
            else:
                sys.modules.pop("rof_providers", None)

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_handles_registry_with_none_cls_gracefully(self, monkeypatch) -> None:
        """Entries with cls=None must be filtered out by the helper."""
        fn = self._import_helper()
        import rof_providers as _rp

        # Inject a broken entry
        fake_registry = dict(getattr(_rp, "PROVIDER_REGISTRY", {}))
        fake_registry["__broken__"] = {"cls": None, "label": "broken", "description": "broken"}
        monkeypatch.setattr(_rp, "PROVIDER_REGISTRY", fake_registry)
        result = fn()
        assert "__broken__" not in result, (
            "_load_generic_providers() must filter out entries with cls=None"
        )


# ---------------------------------------------------------------------------
# 3. Pipeline-factory discovery helper
# ---------------------------------------------------------------------------


class TestPipelineFactoryDiscoveryHelper:
    """Tests for ``bot_service.pipeline_factory._load_generic_providers``."""

    def _import_helper(self):
        try:
            # Add the rof_bot demo root to the path so the import resolves
            import importlib.util

            rof_bot_root = (
                pytest.importorskip.__module__  # just to have a Path import
            )
            from pathlib import Path

            bot_root = Path(__file__).parent.parent / "demos" / "rof_bot"
            if str(bot_root) not in sys.path:
                sys.path.insert(0, str(bot_root))
            from bot_service.pipeline_factory import _load_generic_providers
        except ImportError as exc:
            pytest.skip(f"bot_service.pipeline_factory not importable: {exc}")
        return _load_generic_providers

    def test_returns_dict(self) -> None:
        fn = self._import_helper()
        result = fn()
        assert isinstance(result, dict)

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_returns_non_empty_when_package_installed(self) -> None:
        fn = self._import_helper()
        result = fn()
        assert len(result) > 0

    def test_returns_empty_when_rof_providers_missing(self, monkeypatch) -> None:
        fn = self._import_helper()
        saved = sys.modules.pop("rof_providers", None)
        try:
            monkeypatch.setitem(sys.modules, "rof_providers", None)  # type: ignore[arg-type]
            result = fn()
            assert result == {}
        finally:
            if saved is not None:
                sys.modules["rof_providers"] = saved
            else:
                sys.modules.pop("rof_providers", None)

    @pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
    def test_matches_cli_helper_output(self) -> None:
        """Both helpers must return the same set of provider names."""
        pf_fn = self._import_helper()
        try:
            from rof_framework.cli.main import _load_generic_providers as cli_fn
        except ImportError:
            pytest.skip("rof_framework.cli.main not importable")
        assert set(pf_fn().keys()) == set(cli_fn().keys()), (
            "pipeline_factory and CLI discovery helpers returned different provider sets"
        )


# ---------------------------------------------------------------------------
# 4. conftest generic_providers fixture contract
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
class TestConftestFixture:
    """Verify that the ``generic_providers`` fixture from conftest.py works."""

    def test_fixture_returns_dict(self, generic_providers) -> None:
        assert isinstance(generic_providers, dict)

    def test_fixture_is_non_empty(self, generic_providers) -> None:
        assert len(generic_providers) > 0

    def test_fixture_entries_match_registry(self, generic_providers) -> None:
        for name, spec in generic_providers.items():
            assert name in _REGISTRY, f"Fixture returned '{name}' which is not in PROVIDER_REGISTRY"
            assert spec["cls"] is _REGISTRY[name]["cls"]

    def test_all_fixture_cls_are_llm_providers(self, generic_providers) -> None:
        for name, spec in generic_providers.items():
            assert issubclass(spec["cls"], LLMProvider), name


# ---------------------------------------------------------------------------
# 5. Mock-HTTP complete() round-trip for every registered provider
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ROF_PROVIDERS_AVAILABLE, reason="rof_providers not installed")
class TestGenericProviderCompleteMock:
    """Call ``complete()`` on every registered provider with a mocked HTTP layer.

    The test patches ``httpx.Client`` at the module level so no real network
    call is made.  Each provider is constructed with a dummy API key so the
    constructor's key-validation logic is satisfied.
    """

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_complete_returns_llm_response(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        mock_resp = _make_mock_http_response()
        mock_client = MagicMock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)
        mock_client.post.return_value = mock_resp

        req = LLMRequest(prompt="Hello, provider!", output_mode="raw")

        with patch("httpx.Client", return_value=mock_client):
            result = provider.complete(req)

        assert isinstance(result, LLMResponse), (
            f"Provider '{name}' complete() must return an LLMResponse, got {type(result)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_complete_content_is_string(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        mock_resp = _make_mock_http_response()
        mock_client = MagicMock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)
        mock_client.post.return_value = mock_resp

        req = LLMRequest(prompt="Hello, provider!", output_mode="raw")

        with patch("httpx.Client", return_value=mock_client):
            result = provider.complete(req)

        assert isinstance(result.content, str), (
            f"Provider '{name}' returned non-string content: {type(result.content)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_complete_tool_calls_is_list(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        mock_resp = _make_mock_http_response()
        mock_client = MagicMock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)
        mock_client.post.return_value = mock_resp

        req = LLMRequest(prompt="Hello, provider!", output_mode="raw")

        with patch("httpx.Client", return_value=mock_client):
            result = provider.complete(req)

        assert isinstance(result.tool_calls, list), (
            f"Provider '{name}' returned non-list tool_calls: {type(result.tool_calls)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_complete_raw_is_populated(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        mock_resp = _make_mock_http_response()
        mock_client = MagicMock()
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)
        mock_client.post.return_value = mock_resp

        req = LLMRequest(prompt="Hello, provider!", output_mode="raw")

        with patch("httpx.Client", return_value=mock_client):
            result = provider.complete(req)

        assert result.raw is not None, f"Provider '{name}' returned LLMResponse with raw=None"

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_supports_tool_calling_returns_bool(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        result = provider.supports_tool_calling()
        assert isinstance(result, bool), (
            f"Provider '{name}' supports_tool_calling() must return bool, got {type(result)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_supports_structured_output_returns_bool(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        result = provider.supports_structured_output()
        assert isinstance(result, bool), (
            f"Provider '{name}' supports_structured_output() must return bool, got {type(result)}"
        )

    @pytest.mark.parametrize("name", list(_REGISTRY.keys()))
    def test_context_limit_is_positive_int(self, name: str) -> None:
        spec = _REGISTRY[name]
        cls = spec["cls"]
        constructor_kwargs = _dummy_key_for(spec)

        try:
            provider = cls(**constructor_kwargs)
        except Exception as exc:
            pytest.skip(f"Could not construct provider '{name}': {exc}")

        limit = provider.context_limit
        assert isinstance(limit, int) and limit > 0, (
            f"Provider '{name}' context_limit must be a positive int, got {limit!r}"
        )


# ---------------------------------------------------------------------------
# 6. Live smoke test (skipped unless ROF_TEST_PROVIDER is a registry key)
# ---------------------------------------------------------------------------


@pytest.mark.live_integration
class TestGenericProviderLiveSmoke:
    """Send a real LLM request using the provider resolved by the ``live_llm`` fixture.

    This test class is always collected but automatically skipped when:
      - ``ROF_TEST_PROVIDER`` is not set, or
      - ``ROF_TEST_PROVIDER`` refers to a built-in (non-generic) provider, or
      - ``rof_providers`` is not installed.

    To run against a generic provider:

        ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<key> \\
            pytest tests/test_generic_providers.py -v -m live_integration
    """

    @pytest.fixture(autouse=True)
    def _only_for_generic(self) -> None:
        """Skip when ROF_TEST_PROVIDER is not in the generic registry."""
        import os

        provider = os.environ.get("ROF_TEST_PROVIDER", "").strip().lower()
        if not provider:
            pytest.skip("ROF_TEST_PROVIDER not set")
        if provider not in _REGISTRY:
            pytest.skip(
                f"ROF_TEST_PROVIDER='{provider}' is not a generic registry key — "
                "live smoke test only runs for providers in rof_providers.PROVIDER_REGISTRY"
            )

    def test_live_complete_returns_response(self, live_llm) -> None:
        """A real LLM call returns a non-None LLMResponse."""
        req = LLMRequest(
            prompt=("Respond with exactly one sentence confirming you received this message."),
            max_tokens=64,
            temperature=0.0,
            output_mode="raw",
        )
        result = live_llm.complete(req)
        assert isinstance(result, LLMResponse), (
            f"live_llm.complete() returned {type(result)}, expected LLMResponse"
        )

    def test_live_complete_content_is_non_empty(self, live_llm) -> None:
        """The provider returns a non-empty response to a minimal prompt."""
        req = LLMRequest(
            prompt="Say 'hello'.",
            max_tokens=32,
            temperature=0.0,
            output_mode="raw",
        )
        result = live_llm.complete(req)
        assert result.content.strip(), "live_llm.complete() returned an empty content string"

    def test_live_complete_raw_is_dict(self, live_llm) -> None:
        """The ``raw`` field is a populated dict (the original provider response body)."""
        req = LLMRequest(
            prompt="What is 1 + 1?",
            max_tokens=16,
            temperature=0.0,
            output_mode="raw",
        )
        result = live_llm.complete(req)
        assert isinstance(result.raw, dict) and result.raw, (
            f"live_llm.complete() returned raw={result.raw!r}, expected a non-empty dict"
        )

    def test_live_provider_interface_booleans(self, live_llm) -> None:
        """supports_tool_calling() and supports_structured_output() return bools."""
        assert isinstance(live_llm.supports_tool_calling(), bool)
        assert isinstance(live_llm.supports_structured_output(), bool)

    def test_live_context_limit_is_positive(self, live_llm) -> None:
        """context_limit is a positive integer."""
        limit = live_llm.context_limit
        assert isinstance(limit, int) and limit > 0, (
            f"context_limit={limit!r} is not a positive integer"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
