#!/bin/sh
set -eu

APP_DIR="/app"
RUNTIME_DIR="${APP_RUNTIME_DIR:-/runtime}"
CACHE_DIR="${XDG_CACHE_HOME:-${RUNTIME_DIR}/cache}"
UV_CACHE_DIR="${UV_CACHE_DIR:-${CACHE_DIR}/uv}"
GO_BUILD_CACHE_DIR="${GOCACHE:-${CACHE_DIR}/go-build}"
GO_MODULE_CACHE_DIR="${GOMODCACHE:-${RUNTIME_DIR}/go/pkg/mod}"

mkdir -p \
  "${RUNTIME_DIR}" \
  "${RUNTIME_DIR}/logs" \
  "${RUNTIME_DIR}/smstome_used" \
  "${CACHE_DIR}" \
  "${UV_CACHE_DIR}" \
  "${GO_BUILD_CACHE_DIR}" \
  "${GO_MODULE_CACHE_DIR}"
touch \
  "${RUNTIME_DIR}/account_manager.db" \
  "${RUNTIME_DIR}/smstome_all_numbers.txt" \
  "${RUNTIME_DIR}/smstome_uk_deep_numbers.txt" \
  "${RUNTIME_DIR}/logs/solver.log"

ln -sfn "${RUNTIME_DIR}/account_manager.db" "${APP_DIR}/account_manager.db"
ln -sfn "${RUNTIME_DIR}/smstome_used" "${APP_DIR}/smstome_used"
ln -sfn "${RUNTIME_DIR}/smstome_all_numbers.txt" "${APP_DIR}/smstome_all_numbers.txt"
ln -sfn "${RUNTIME_DIR}/smstome_uk_deep_numbers.txt" "${APP_DIR}/smstome_uk_deep_numbers.txt"
ln -sfn "${RUNTIME_DIR}/logs/solver.log" "${APP_DIR}/services/turnstile_solver/solver.log"

echo "[entrypoint] Starting backend under Xvfb so Docker can handle both headed and headless browser tasks"
exec xvfb-run -a --server-args="-screen 0 1920x1080x24" uv run main.py
