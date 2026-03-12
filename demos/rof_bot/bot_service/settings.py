"""
bot_service/settings.py
=======================
Pydantic-based settings loaded from environment variables / .env file.

All values have sensible defaults so the bot starts in dry-run / SQLite mode
without any external services.  Override via environment variables or a .env
file in the working directory.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Optional Pydantic v2 import — fall back to a plain dataclass if not installed
# ---------------------------------------------------------------------------
try:
    from pydantic import Field, field_validator, model_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYDANTIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CycleTrigger(str, Enum):
    INTERVAL = "interval"
    CRON = "cron"
    EVENT = "event"


class DryRunMode(str, Enum):
    LOG_ONLY = "log_only"
    MOCK_ACTIONS = "mock_actions"
    SHADOW = "shadow"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Settings implementation — Pydantic v2 when available, plain fallback otherwise
# ---------------------------------------------------------------------------

if _PYDANTIC_AVAILABLE:

    class Settings(BaseSettings):
        """
        ROF Bot runtime configuration.

        All fields are read from environment variables (case-insensitive).
        A ``.env`` file in the current working directory is also loaded
        automatically when pydantic-settings is installed.

        Usage
        -----
            from bot_service.settings import get_settings
            settings = get_settings()
        """

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
        )

        # ── LLM Provider ────────────────────────────────────────────────
        rof_provider: str = Field(default="anthropic", description="LLM provider name")
        rof_model: str = Field(default="claude-sonnet-4-6", description="Default LLM model")
        rof_api_key: str = Field(default="", description="LLM provider API key")
        rof_decide_model: str = Field(
            default="claude-opus-4-6",
            description="Powerful model used for the decide stage only",
        )

        # ── External system credentials ─────────────────────────────────
        external_api_key: str = Field(default="", description="Primary external system API key")
        external_api_base_url: str = Field(
            default="https://api.example.com",
            description="Primary external system base URL",
        )
        external_signal_api_key: str = Field(default="", description="Signal source API key")
        external_signal_base_url: str = Field(
            default="https://signals.example.com",
            description="Signal source base URL",
        )
        signal_cache_ttl_seconds: int = Field(
            default=300,
            description="Redis TTL for cached external signals (seconds)",
        )

        # ── Storage ─────────────────────────────────────────────────────
        database_url: str = Field(
            default="sqlite:///./rof_bot.db",
            description=(
                "SQLAlchemy DSN.  SQLite by default (no extra services needed). "
                "Switch to postgresql://... for production."
            ),
        )
        async_database_url: Optional[str] = Field(
            default=None,
            description=(
                "Async SQLAlchemy DSN (asyncpg driver).  "
                "Derived from database_url automatically when not set explicitly."
            ),
        )
        redis_url: str = Field(
            default="redis://localhost:6379/0",
            description="Redis connection URL",
        )
        chromadb_path: str = Field(
            default="./data/chromadb",
            description="ChromaDB persistence directory",
        )

        # ── Bot behaviour ────────────────────────────────────────────────
        bot_cycle_trigger: CycleTrigger = Field(
            default=CycleTrigger.INTERVAL,
            description="What drives a new cycle: interval | cron | event",
        )
        bot_cycle_interval_seconds: int = Field(
            default=60,
            ge=1,
            description="Seconds between cycles when trigger=interval",
        )
        bot_cycle_cron: str = Field(
            default="",
            description="Cron expression when trigger=cron, e.g. '*/5 * * * *'",
        )
        bot_targets: str = Field(
            default="target_a",
            description="Comma-separated list of subjects to fan-out per cycle",
        )
        bot_dry_run: bool = Field(
            default=True,
            description="Master dry-run switch — ActionExecutorTool never executes live when True",
        )
        bot_dry_run_mode: DryRunMode = Field(
            default=DryRunMode.LOG_ONLY,
            description="Dry-run behaviour: log_only | mock_actions | shadow",
        )

        # ── Operational limits ───────────────────────────────────────────
        bot_max_concurrent_actions: int = Field(
            default=5,
            ge=1,
            description="Maximum simultaneously active external actions",
        )
        bot_daily_error_budget: float = Field(
            default=0.05,
            ge=0.0,
            le=1.0,
            description="Fraction of daily cycles allowed to fail before guardrail fires",
        )
        bot_resource_utilisation_limit: float = Field(
            default=0.80,
            ge=0.0,
            le=1.0,
            description="Generic capacity cap — bot throttles when exceeded",
        )

        # ── Security ─────────────────────────────────────────────────────
        operator_key: str = Field(
            default="change-me-in-production",
            description="Secret required in X-Operator-Key header for emergency-stop",
        )
        api_key: str = Field(
            default="",
            description="Bearer token required for /control write endpoints (empty = disabled)",
        )

        # ── Observability ────────────────────────────────────────────────
        prometheus_port: int = Field(default=9090, description="Prometheus scrape port")
        grafana_port: int = Field(default=3000, description="Grafana UI port")
        log_level: LogLevel = Field(default=LogLevel.INFO, description="Root log level")

        # ── Service ──────────────────────────────────────────────────────
        host: str = Field(default="0.0.0.0", description="FastAPI bind host")
        port: int = Field(default=8080, description="FastAPI bind port")
        routing_memory_checkpoint_minutes: int = Field(
            default=5,
            ge=1,
            description="How often routing memory is persisted to the database",
        )

        # ── Derived helpers ──────────────────────────────────────────────

        @field_validator("bot_cycle_cron")
        @classmethod
        def _validate_cron(cls, v: str) -> str:
            # Accept empty string — only validated when trigger=cron
            return v.strip()

        @model_validator(mode="after")
        def _derive_async_database_url(self) -> "Settings":
            """
            Auto-derive the async DSN from the sync DSN when not explicitly set.

            postgresql://...  →  postgresql+asyncpg://...
            sqlite:///...     →  left as-is (aiosqlite not required for the service)
            """
            if self.async_database_url is None:
                sync = self.database_url
                if sync.startswith("postgresql://"):
                    self.async_database_url = sync.replace(
                        "postgresql://", "postgresql+asyncpg://", 1
                    )
                elif sync.startswith("postgresql+psycopg2://"):
                    self.async_database_url = sync.replace(
                        "postgresql+psycopg2://", "postgresql+asyncpg://", 1
                    )
                else:
                    self.async_database_url = sync
            return self

        # ── Convenience properties ───────────────────────────────────────

        @property
        def targets_list(self) -> list[str]:
            """Return bot_targets as a parsed list."""
            return [t.strip() for t in self.bot_targets.split(",") if t.strip()]

        @property
        def is_postgres(self) -> bool:
            """True when the configured DSN points at PostgreSQL."""
            return self.database_url.startswith("postgresql")

        @property
        def is_multi_target(self) -> bool:
            """True when more than one target is configured."""
            return len(self.targets_list) > 1

else:
    # ---------------------------------------------------------------------------
    # Plain-Python fallback when pydantic / pydantic-settings is not installed.
    # Reads directly from os.environ / a .env file loaded manually.
    # ---------------------------------------------------------------------------

    def _load_dotenv(path: str = ".env") -> None:
        """Minimal .env loader — does not override existing env vars."""
        env_path = Path(path)
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.upper(), val)

    _load_dotenv()

    class Settings:  # type: ignore[no-redef]
        """Fallback settings implementation without Pydantic."""

        def __init__(self) -> None:
            e = os.environ

            self.rof_provider = e.get("ROF_PROVIDER", "anthropic")
            self.rof_model = e.get("ROF_MODEL", "claude-sonnet-4-6")
            self.rof_api_key = e.get("ROF_API_KEY", "")
            self.rof_decide_model = e.get("ROF_DECIDE_MODEL", "claude-opus-4-6")

            self.external_api_key = e.get("EXTERNAL_API_KEY", "")
            self.external_api_base_url = e.get("EXTERNAL_API_BASE_URL", "https://api.example.com")
            self.external_signal_api_key = e.get("EXTERNAL_SIGNAL_API_KEY", "")
            self.external_signal_base_url = e.get(
                "EXTERNAL_SIGNAL_BASE_URL", "https://signals.example.com"
            )
            self.signal_cache_ttl_seconds = int(e.get("SIGNAL_CACHE_TTL_SECONDS", "300"))

            self.database_url = e.get("DATABASE_URL", "sqlite:///./rof_bot.db")
            self.redis_url = e.get("REDIS_URL", "redis://localhost:6379/0")
            self.chromadb_path = e.get("CHROMADB_PATH", "./data/chromadb")

            # Derive async URL
            _async_url = e.get("ASYNC_DATABASE_URL", "")
            if not _async_url:
                sync = self.database_url
                if sync.startswith("postgresql://"):
                    _async_url = sync.replace("postgresql://", "postgresql+asyncpg://", 1)
                elif sync.startswith("postgresql+psycopg2://"):
                    _async_url = sync.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
                else:
                    _async_url = sync
            self.async_database_url = _async_url

            self.bot_cycle_trigger = CycleTrigger(
                e.get("BOT_CYCLE_TRIGGER", CycleTrigger.INTERVAL.value)
            )
            self.bot_cycle_interval_seconds = int(e.get("BOT_CYCLE_INTERVAL_SECONDS", "60"))
            self.bot_cycle_cron = e.get("BOT_CYCLE_CRON", "").strip()
            self.bot_targets = e.get("BOT_TARGETS", "target_a")
            self.bot_dry_run = e.get("BOT_DRY_RUN", "true").lower() in ("1", "true", "yes")
            self.bot_dry_run_mode = DryRunMode(e.get("BOT_DRY_RUN_MODE", DryRunMode.LOG_ONLY.value))

            self.bot_max_concurrent_actions = int(e.get("BOT_MAX_CONCURRENT_ACTIONS", "5"))
            self.bot_daily_error_budget = float(e.get("BOT_DAILY_ERROR_BUDGET", "0.05"))
            self.bot_resource_utilisation_limit = float(
                e.get("BOT_RESOURCE_UTILISATION_LIMIT", "0.80")
            )

            self.operator_key = e.get("OPERATOR_KEY", "change-me-in-production")
            self.api_key = e.get("API_KEY", "")

            self.prometheus_port = int(e.get("PROMETHEUS_PORT", "9090"))
            self.grafana_port = int(e.get("GRAFANA_PORT", "3000"))
            self.log_level = LogLevel(e.get("LOG_LEVEL", LogLevel.INFO.value))

            self.host = e.get("HOST", "0.0.0.0")
            self.port = int(e.get("PORT", "8080"))
            self.routing_memory_checkpoint_minutes = int(
                e.get("ROUTING_MEMORY_CHECKPOINT_MINUTES", "5")
            )

        @property
        def targets_list(self) -> list[str]:
            return [t.strip() for t in self.bot_targets.split(",") if t.strip()]

        @property
        def is_postgres(self) -> bool:
            return self.database_url.startswith("postgresql")

        @property
        def is_multi_target(self) -> bool:
            return len(self.targets_list) > 1


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

if _PYDANTIC_AVAILABLE:

    @lru_cache(maxsize=1)
    def get_settings() -> Settings:
        """
        Return the singleton Settings instance.

        The result is cached — any call after the first returns the same object.
        In tests, call ``get_settings.cache_clear()`` then patch env vars before
        creating a new instance.
        """
        return Settings()
else:
    _settings_singleton: Optional[Settings] = None

    def get_settings() -> Settings:  # type: ignore[misc]
        global _settings_singleton
        if _settings_singleton is None:
            _settings_singleton = Settings()
        return _settings_singleton
