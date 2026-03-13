# ============================================================================
# ROF Bot Dashboard — Dockerfile
# ============================================================================
# Multi-stage build:
#   Stage 1 (builder) — installs dependencies and runs Vite production build
#   Stage 2 (runtime) — minimal nginx image serving the static SPA
#
# Build:
#   docker build -t rof-bot-dashboard:latest .
#
# Run:
#   docker run -p 3000:80 \
#     -e VITE_API_BASE=http://bot-service:8080 \
#     rof-bot-dashboard:latest
#
# The nginx config proxies /api/* to the backend so the SPA can use relative
# paths in development and absolute paths in production via VITE_API_BASE.
# ============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — Builder
# ---------------------------------------------------------------------------
FROM node:20-alpine AS builder

LABEL stage="builder"

WORKDIR /app

# Copy package manifests first for layer caching
COPY package.json package-lock.json* ./

# Install all dependencies (including devDependencies needed for the build)
RUN npm ci --ignore-scripts

# Copy the rest of the source
COPY . .

# Build arguments — can be overridden at build time
ARG VITE_API_BASE=""
ENV VITE_API_BASE=${VITE_API_BASE}

# Type-check + build
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — Runtime (nginx)
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

LABEL maintainer="ROF Bot Team"
LABEL description="ROF Bot Operator Dashboard — static SPA served by nginx"

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Install envsubst (part of gettext) for runtime env variable injection
# (available in alpine's gettext package)
RUN apk add --no-cache gettext

# Copy custom nginx configuration template
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Copy built static assets from the builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# nginx runs as non-root
RUN chown -R nginx:nginx /usr/share/nginx/html \
 && chown -R nginx:nginx /var/cache/nginx \
 && chown -R nginx:nginx /var/log/nginx \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid

# Ensure the html dir is readable
RUN chmod -R 755 /usr/share/nginx/html

# Expose HTTP port
EXPOSE 80

# Health check — nginx responds on /
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:80/ > /dev/null || exit 1

USER nginx

# nginx:alpine's official image already uses CMD ["nginx", "-g", "daemon off;"]
# The /etc/nginx/templates/ directory is processed by the entrypoint script
# using envsubst before nginx starts.
CMD ["nginx", "-g", "daemon off;"]
