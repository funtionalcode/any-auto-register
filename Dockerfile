# syntax=docker/dockerfile:1.7

# Stage 1: Build Python runtime (placed first to leverage layer caching)
FROM python:3.12-slim AS runtime

ARG CAMOUFOX_VERSION=135.0.1
ARG CAMOUFOX_RELEASE=beta.24
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG all_proxy
ARG no_proxy

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
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    ALL_PROXY=${ALL_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    all_proxy=${all_proxy} \
    no_proxy=${no_proxy} \
    PATH=/usr/local/go/bin:$PATH:/root/.local/bin

WORKDIR /app

COPY requirements.txt ./
COPY scripts/install_camoufox.py /tmp/install_camoufox.py

# Install system dependencies
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
RUN apt-get update
RUN apt-get install -y --no-install-recommends \
    curl git net-tools vim telnet \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LO https://go.dev/dl/go1.24.0.linux-amd64.tar.gz \
    && tar -C /usr/local -xzf go1.24.0.linux-amd64.tar.gz \
    && rm go1.24.0.linux-amd64.tar.gz \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

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

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl git net-tools vim telnet \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Stage 2: Build frontend (placed after runtime to avoid rebuilding on code changes)
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

COPY frontend/ ./
RUN npm run build


# Stage 3: Final image
FROM runtime

COPY --from=frontend-builder /app/static /app/static

RUN apt-get update && apt-get install -y --no-install-recommends dos2unix git iproute2 procps \
    && dos2unix /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /runtime /runtime/logs /runtime/smstome_used /_ext_targets \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8000 8889 8317 8011

VOLUME ["/runtime", "/_ext_targets"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
