# syntax=docker/dockerfile:1.7

# ============================================================
# Stage 1: Python runtime base (heavy, cached by requirements.txt)
# ============================================================
FROM python:3.12-slim AS runtime-base

ARG CAMOUFOX_VERSION=135.0.1
ARG CAMOUFOX_RELEASE=beta.24

# Only non-proxy env vars — proxy ARGs deliberately NOT set here
# so that proxy changes don't invalidate apt/pip layers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    APP_TIMEZONE=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_CONDA_ENV=docker \
    APP_RELOAD=0 \
    APP_RUNTIME_DIR=/runtime \
    APP_ENABLE_SOLVER=1 \
    SOLVER_PORT=8889 \
    SOLVER_BIND_HOST=0.0.0.0 \
    LOCAL_SOLVER_URL=http://127.0.0.1:8889 \
    SOLVER_BROWSER_TYPE=camoufox \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/usr/local/go/bin:$PATH:/root/.local/bin

WORKDIR /app

# --- System deps (first thing, never invalidated by proxy/code changes) ---
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       curl git net-tools vim telnet \
       dos2unix iproute2 procps xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

# --- Go + uv (also never invalidated by proxy/code changes) ---
RUN curl -LO https://go.dev/dl/go1.24.0.linux-amd64.tar.gz \
    && tar -C /usr/local -xzf go1.24.0.linux-amd64.tar.gz \
    && rm go1.24.0.linux-amd64.tar.gz \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

# --- Python deps (cached by requirements.txt; proxy only used in this RUN) ---
# Declare proxy ARGs as late as possible — before this RUN only
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG all_proxy
ARG no_proxy

COPY requirements.txt ./
COPY scripts/install_camoufox.py /tmp/install_camoufox.py

RUN set -eux; \
    export HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}" \
      HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}" \
      ALL_PROXY="${ALL_PROXY:-${all_proxy:-}}" \
      NO_PROXY="${NO_PROXY:-${no_proxy:-}}" \
      http_proxy="${http_proxy:-${HTTP_PROXY:-}}" \
      https_proxy="${https_proxy:-${HTTPS_PROXY:-}}" \
      all_proxy="${all_proxy:-${ALL_PROXY:-}}" \
      no_proxy="${no_proxy:-${NO_PROXY:-}}"; \
    pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install-deps firefox chromium \
    && installed=0 \
    && for attempt in 1 2 3; do \
         if python -m playwright install --with-deps chromium; then \
           installed=1; \
           break; \
         fi; \
         if [ "$attempt" -eq 3 ]; then break; fi; \
         echo "playwright browser install failed, retrying ($attempt/3)..." >&2; \
         sleep 5; \
       done \
    && [ "$installed" -eq 1 ] \
    && CAMOUFOX_VERSION="$CAMOUFOX_VERSION" CAMOUFOX_RELEASE="$CAMOUFOX_RELEASE" python /tmp/install_camoufox.py

# Set runtime proxy env (after heavy installs are done)
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    ALL_PROXY=${ALL_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    all_proxy=${all_proxy} \
    no_proxy=${no_proxy}


# ============================================================
# Stage 2: Frontend builder (cached by package-lock.json)
# ============================================================
FROM node:20-bookworm-slim AS frontend-builder

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG all_proxy
ARG no_proxy

ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    ALL_PROXY=${ALL_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    all_proxy=${all_proxy} \
    no_proxy=${no_proxy}

WORKDIR /app/frontend

# npm deps — only re-run when package-lock.json changes
COPY frontend/package.json frontend/package-lock.json ./
RUN set -eux; \
    export HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}" \
      HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}" \
      ALL_PROXY="${ALL_PROXY:-${all_proxy:-}}" \
      NO_PROXY="${NO_PROXY:-${no_proxy:-}}" \
      http_proxy="${http_proxy:-${HTTP_PROXY:-}}" \
      https_proxy="${https_proxy:-${HTTPS_PROXY:-}}" \
      all_proxy="${all_proxy:-${ALL_PROXY:-}}" \
      no_proxy="${no_proxy:-${NO_PROXY:-}}"; \
    if [ -n "${HTTP_PROXY:-}" ]; then npm config set proxy "$HTTP_PROXY"; fi; \
    if [ -n "${HTTPS_PROXY:-}" ]; then npm config set https-proxy "$HTTPS_PROXY"; fi; \
    npm ci

# frontend source — only re-build when src changes
COPY frontend/ ./
RUN npm run build


# ============================================================
# Stage 3: Final image (lightweight — only source + frontend)
# ============================================================
FROM runtime-base

# Copy backend source in targeted layers (most stable → most volatile)

# 1) Config & scripts (rarely change)
COPY check_config.py smstome_tool.py ./
COPY scripts/ ./scripts/
COPY docker/ ./docker/

# 2) Core library (changes less often than api/)
COPY core/ ./core/

# 3) Platform plugins
COPY platforms/ ./platforms/

# 4) Services
COPY services/ ./services/

# 5) API routes (changes most frequently)
COPY api/ ./api/

# 6) App entrypoint
COPY main.py ./

# 7) Frontend build output (from builder stage)
COPY --from=frontend-builder /app/static /app/static

# Finalize runtime
RUN dos2unix /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /runtime /runtime/logs /runtime/smstome_used /_ext_targets

EXPOSE 8000 8889 8317 8011

VOLUME ["/runtime", "/_ext_targets"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
