"""全局 HTTP 请求日志中间件。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config_store import config_store
from core.log_utils import configure_request_logging


REQUEST_LOGGER = logging.getLogger("http.request")
REQUEST_LOGGING_ENABLED_KEY = "request_logging_enabled"
_MASKED_VALUE = "***"
_MAX_BODY_BYTES = 64 * 1024
_MAX_TEXT_CHARS = 8000
_SKIP_PATHS = {
    "/api/request/logs",
    "/api/runtime/logs",
    "/api/solver/logs",
}
_SKIP_PREFIXES = ("/assets/",)
_LOG_PATH_PREFIXES = ("/api", "/v1", "/apps/anthropic")
_BINARY_CONTENT_MARKERS = (
    "application/octet-stream",
    "application/pdf",
    "image/",
    "audio/",
    "video/",
    "font/",
    "multipart/form-data",
)
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
}


def _to_bool(value: Any, *, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def is_request_logging_enabled(*, default: bool = False) -> bool:
    try:
        return _to_bool(config_store.get(REQUEST_LOGGING_ENABLED_KEY, ""), default=default)
    except Exception:
        return default


def _is_sensitive_name(name: str) -> bool:
    text = str(name or "").strip().lower().replace("-", "_")
    if not text:
        return False
    if text in {"auth", "authorization", "cookie", "set_cookie", "token", "secret", "password", "passwd", "pwd", "api_key", "key"}:
        return True
    if text.endswith("_key") or text.endswith("_token") or text.endswith("_secret") or text.endswith("_password") or text.endswith("_auth"):
        return True
    return any(marker in text for marker in ("password", "passwd", "pwd", "token", "secret", "cookie", "authorization"))


def _mask_value(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return _MASKED_VALUE


def _sanitize_value(value: Any, *, field_name: str = "") -> Any:
    if _is_sensitive_name(field_name):
        return _mask_value(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, field_name=field_name) for item in value]
    return value


def _truncate_text(text: str, *, max_chars: int = _MAX_TEXT_CHARS) -> str:
    clean = str(text or "")
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars]}\n...<truncated>"


def _decode_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    items: dict[str, str] = {}
    for key_bytes, value_bytes in raw_headers or []:
        key = key_bytes.decode("latin-1", errors="ignore").strip().lower()
        value = value_bytes.decode("latin-1", errors="ignore").strip()
        if not key:
            continue
        next_value = _MASKED_VALUE if key in _SENSITIVE_HEADERS or _is_sensitive_name(key) else value
        if key in items and items[key] != next_value:
            current = items[key]
            if isinstance(current, str):
                items[key] = f"{current}, {next_value}"
            continue
        items[key] = next_value
    return items


def _decode_query(query_string: bytes) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in parse_qsl(query_string.decode("utf-8", errors="ignore"), keep_blank_values=True):
        safe_value = _mask_value(value) if _is_sensitive_name(key) else value
        if key in values:
            current = values[key]
            if isinstance(current, list):
                current.append(safe_value)
            else:
                values[key] = [current, safe_value]
        else:
            values[key] = safe_value
    return values


def _content_type(headers: dict[str, str]) -> str:
    return str(headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()


def _format_body_preview(body: bytes, *, content_type: str) -> str:
    if not body:
        return ""

    if any(content_type.startswith(marker) for marker in _BINARY_CONTENT_MARKERS):
        return f"<{content_type or 'binary'} body omitted: {len(body)} bytes>"

    text = body.decode("utf-8", errors="ignore")
    if not text.strip():
        return ""

    if content_type == "application/json":
        try:
            parsed = json.loads(text)
            return _truncate_text(json.dumps(_sanitize_value(parsed), ensure_ascii=False, indent=2))
        except Exception:
            return _truncate_text(text)

    if content_type == "application/x-www-form-urlencoded":
        parsed = _decode_query(body)
        return _truncate_text(json.dumps(parsed, ensure_ascii=False, indent=2))

    return _truncate_text(text)


def _extract_client_ip(scope: Scope, headers: dict[str, str]) -> str:
    forwarded_for = str(headers.get("x-forwarded-for", "") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return str(client[0] or "")
    return ""


def _should_skip_request(scope: Scope) -> bool:
    path = str(scope.get("path", "") or "")
    method = str(scope.get("method", "") or "").upper()
    if method == "OPTIONS":
        return True
    if not any(path == prefix or path.startswith(f"{prefix}/") for prefix in _LOG_PATH_PREFIXES):
        return True
    if path in _SKIP_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


async def _read_request_body(receive: Receive) -> tuple[bytes, Message | None]:
    chunks: list[bytes] = []
    disconnect_message: Message | None = None

    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            disconnect_message = message
            break
        if message["type"] != "http.request":
            continue
        body = message.get("body", b"")
        if body:
            chunks.append(body)
        if not message.get("more_body", False):
            break

    return b"".join(chunks), disconnect_message


def _build_receive(request_body: bytes, disconnect_message: Message | None) -> Receive:
    sent_body = False
    sent_disconnect = False

    async def _receive() -> Message:
        nonlocal sent_body, sent_disconnect
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        if disconnect_message is not None and not sent_disconnect:
            sent_disconnect = True
            return disconnect_message
        return {"type": "http.request", "body": b"", "more_body": False}

    return _receive


def _compose_log_message(
    *,
    scope: Scope,
    request_headers: dict[str, str],
    request_body: bytes,
    response_headers: dict[str, str],
    response_body: bytes,
    response_body_truncated: bool,
    status_code: int,
    duration_ms: int,
    error: str = "",
) -> str:
    method = str(scope.get("method", "") or "").upper()
    path = str(scope.get("path", "") or "")
    query = _decode_query(scope.get("query_string", b""))
    request_content_type = _content_type(request_headers)
    response_content_type = _content_type(response_headers)
    request_preview = _format_body_preview(request_body, content_type=request_content_type)
    response_preview = _format_body_preview(response_body, content_type=response_content_type)
    client_ip = _extract_client_ip(scope, request_headers)
    user_agent = request_headers.get("user-agent", "")

    lines = [
        f"[HTTP] {method} {path} -> {status_code} ({duration_ms}ms)",
        f"Client: ip={client_ip or '-'} ua={user_agent or '-'}",
        f"Request Headers:\n{json.dumps(request_headers, ensure_ascii=False, indent=2)}",
    ]

    if query:
        lines.append(f"Query:\n{json.dumps(query, ensure_ascii=False, indent=2)}")
    if request_preview:
        lines.append(f"Request Body:\n{request_preview}")
    else:
        lines.append("Request Body:\n<empty>")

    lines.append(f"Response Headers:\n{json.dumps(response_headers, ensure_ascii=False, indent=2)}")
    if response_preview:
        suffix = "\n<truncated>" if response_body_truncated else ""
        lines.append(f"Response Body:\n{response_preview}{suffix}")
    else:
        lines.append("Response Body:\n<empty>")

    if error:
        lines.append(f"Error:\n{_truncate_text(error)}")

    return "\n".join(lines)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        configure_request_logging()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _should_skip_request(scope) or not is_request_logging_enabled(default=False):
            await self.app(scope, receive, send)
            return

        request_headers = _decode_headers(scope.get("headers", []))
        request_body, disconnect_message = await _read_request_body(receive)
        replay_receive = _build_receive(request_body, disconnect_message)
        started_at = time.perf_counter()
        response_status = 500
        response_headers: dict[str, str] = {}
        response_chunks: list[bytes] = []
        response_body_size = 0
        response_body_truncated = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_status, response_headers, response_body_size, response_body_truncated
            if message["type"] == "http.response.start":
                response_status = int(message.get("status", 500) or 500)
                response_headers = _decode_headers(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body = message.get("body", b"") or b""
                captured_before = response_body_size
                if body and response_body_size < _MAX_BODY_BYTES:
                    remaining = _MAX_BODY_BYTES - response_body_size
                    take = min(len(body), remaining)
                    response_chunks.append(body[:take])
                    response_body_size += take
                if body and (captured_before >= _MAX_BODY_BYTES or captured_before + len(body) > _MAX_BODY_BYTES):
                    response_body_truncated = True
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            REQUEST_LOGGER.info(
                _compose_log_message(
                    scope=scope,
                    request_headers=request_headers,
                    request_body=request_body,
                    response_headers=response_headers,
                    response_body=b"".join(response_chunks),
                    response_body_truncated=response_body_truncated,
                    status_code=response_status,
                    duration_ms=duration_ms,
                )
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            REQUEST_LOGGER.exception(
                _compose_log_message(
                    scope=scope,
                    request_headers=request_headers,
                    request_body=request_body,
                    response_headers=response_headers,
                    response_body=b"".join(response_chunks),
                    response_body_truncated=response_body_truncated,
                    status_code=response_status,
                    duration_ms=duration_ms,
                    error=str(exc),
                )
            )
            raise
