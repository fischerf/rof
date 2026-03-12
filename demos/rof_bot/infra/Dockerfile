# infra/Dockerfile
# ROF Bot Service — Docker image
#
# Build context: D:/Github/rof/ (project root)
# Build command (from project root):
#   docker build -f demos/rof_bot/infra/Dockerfile -t rof-bot:latest .
#
# Or via docker compose:
#   docker compose -f demos/rof_bot/infra/docker-compose.yml build
#
# Multi-stage build:
#   builder  — installs all dependencies into a virtualenv
#   runtime  — copies only the venv + source, no build tools
#
# The image runs as a non-root user (botuser) for security.

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Build-time dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Create virtualenv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip first
RUN pip install --upgrade pip setuptools wheel

# ── Install rof_framework (the core package) ──────────────────────────────────
# Copy the minimal files needed for package installation first
# (leverages Docker layer cache — only rebuilds when pyproject.toml changes)
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install rof_framework with all optional extras
RUN pip install -e ".[all]" --no-cache-dir

# ── Install bot service dependencies ──────────────────────────────────────────
# These are the dependencies declared in the bot's own requirements
RUN pip install --no-cache-dir \
    # Web framework
    fastapi>=0.111 \
    "uvicorn[standard]>=0.30" \
    # Scheduler
    "apscheduler>=3.10" \
    # Database — SQLAlchemy with async support
    "sqlalchemy>=2.0" \
    aiosqlite \
    asyncpg \
    psycopg2-binary \
    # Redis
    "redis[asyncio]" \
    # HTTP client
    "httpx>=0.27" \
    # Vector store
    chromadb \
    "sentence-transformers>=3.0" \
    # Metrics
    "prometheus-client>=0.20" \
    # Config
    pyyaml \
    python-dotenv \
    pydantic-settings \
    pydantic

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime system dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 botuser \
    && useradd --uid 1000 --gid botuser --shell /bin/bash --create-home botuser

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy rof_framework source
COPY --chown=botuser:botuser src/ ./src/
COPY --chown=botuser:botuser pyproject.toml README.md ./

# Install rof_framework in the runtime venv (editable, uses source tree)
RUN pip install -e ".[all]" --no-cache-dir --no-build-isolation

# Copy the rof_bot demo
COPY --chown=botuser:botuser demos/rof_bot/ ./demos/rof_bot/

# Create data directories with correct ownership
RUN mkdir -p /data/chromadb /app/logs \
    && chown -R botuser:botuser /data /app/logs

# Switch to non-root user
USER botuser

# Environment defaults
# Override via docker-compose env_file or -e flags
ENV PYTHONPATH="/app/src:/app/demos/rof_bot" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Service defaults (override via .env)
    HOST=0.0.0.0 \
    PORT=8080 \
    LOG_LEVEL=INFO \
    # Safety defaults — always start in dry-run mode
    BOT_DRY_RUN=true \
    BOT_DRY_RUN_MODE=log_only \
    # SQLite by default (overridden by docker-compose to postgresql)
    DATABASE_URL=sqlite:////app/data/rof_bot.db \
    CHROMADB_PATH=/data/chromadb

# Expose the FastAPI port
EXPOSE 8080

# Health check — uses the /health endpoint added in main.py
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Working directory for the service
WORKDIR /app/demos/rof_bot

# Default command
# Workers=1 is intentional — the APScheduler and asyncio.Lock patterns
# require a single process.  Scale horizontally via separate deployments
# with different BOT_TARGETS values, not via --workers > 1.
CMD ["uvicorn", "bot_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info", \
     "--access-log"]
