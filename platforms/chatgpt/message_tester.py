from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit

from .chatgpt_client import ChatGPTClient
from .sentinel_token import SentinelTokenGenerator
from .token_refresh import TokenRefreshManager


TEST_PROMPT = "Reply with exactly TEST_OK"
TEST_PROMPT = "Reply with exactly haogege"
logger = logging.getLogger(__name__)

_DEBUG_RESPONSE_HEADERS = {
    "content-type",
    "cf-ray",
    "openai-model",
    "server",
    "transfer-encoding",
    "x-request-id",
}


@dataclass
class ChatGPTMessageTestResult:
    ok: bool
    invalid: bool
    message: str
    response_excerpt: str = ""
    response_text: str = ""
    model: str = "auto"
    conversation_id: str = ""
    response_message_id: str = ""
    used_proxy: str = ""
    updated_access_token: str = ""
    updated_refresh_token: str = ""


@dataclass
class ChatGPTModelsResult:
    ok: bool
    invalid: bool
    message: str
    used_proxy: str = ""
    models_url: str = ""
    models: list[dict[str, str]] = field(default_factory=list)
    data: Any = None
    updated_access_token: str = ""
    updated_refresh_token: str = ""
    response_status_code: int = 0


@dataclass
class ChatGPTQuotaResult:
    ok: bool
    invalid: bool
    message: str
    used_proxy: str = ""
    query_url: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    signals: list[dict[str, Any]] = field(default_factory=list)
    data: Any = None
    updated_access_token: str = ""
    updated_refresh_token: str = ""
    response_status_code: int = 0


@dataclass
class PreparedChatRequest:
    client: ChatGPTClient
    access_token: str
    updated_access_token: str
    updated_refresh_token: str
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


def _truncate_text(value: Any, limit: int = 400) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _sanitize_proxy(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value or "://" not in value or "@" not in value:
        return value

    scheme, remainder = value.split("://", 1)
    credentials, host = remainder.rsplit("@", 1)
    username = credentials.split(":", 1)[0]
    return f"{scheme}://{username}:***@{host}"


def _exception_message(exc: Exception, fallback: str) -> str:
    text = str(exc or "").strip()
    if text:
        return text
    exc_type = type(exc).__name__.strip()
    if exc_type and exc_type != "Exception":
        return f"{fallback}: {exc_type}"
    return fallback


@contextmanager
def _open_streaming_response(session: Any, method: str, url: str, **kwargs: Any):
    stream_method = getattr(session, "stream", None)
    if callable(stream_method):
        with stream_method(method, url, **kwargs) as response:
            yield response
        return

    request_method = getattr(session, str(method or "POST").lower(), None)
    if not callable(request_method):
        raise RuntimeError(f"session missing method: {method}")

    response = request_method(url, stream=True, **kwargs)
    try:
        yield response
    finally:
        try:
            response.close()
        except Exception:
            pass


def _iter_stream_lines(response: Any) -> Iterator[str]:
    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if isinstance(raw_line, bytes):
                yield raw_line.decode("utf-8", errors="ignore")
            else:
                yield str(raw_line)
        return
    except NotImplementedError:
        pass

    try:
        buffer = ""
        for chunk in response.iter_content():
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="ignore")
            else:
                text = str(chunk or "")
            if not text:
                continue
            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line.rstrip("\r")
        if buffer:
            yield buffer.rstrip("\r")
        return
    except NotImplementedError:
        pass

    text = str(getattr(response, "text", "") or "")
    for line in text.splitlines():
        yield line


def _response_header_snapshot(response: Any) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    headers = getattr(response, "headers", None)
    if headers is None:
        return snapshot

    try:
        for key, value in headers.items():
            normalized_key = str(key).lower()
            if normalized_key not in _DEBUG_RESPONSE_HEADERS:
                continue
            snapshot[normalized_key] = _truncate_text(value, limit=200)
    except Exception:
        return snapshot

    return snapshot


def _log_http_response(chain_name: str, response: Any, *, note: str, body_excerpt: str = "") -> None:
    logger.info(
        "[chatgpt-message] chain=%s note=%s status=%s headers=%s",
        chain_name,
        note,
        getattr(response, "status_code", ""),
        _response_header_snapshot(response),
    )
    if body_excerpt:
        logger.warning(
            "[chatgpt-message] chain=%s note=%s body_excerpt=%s",
            chain_name,
            note,
            body_excerpt,
        )


def _set_session_cookie(client: ChatGPTClient, name: str, value: str, domain: str = ".chatgpt.com") -> None:
    if not value:
        return
    client.session.cookies.set(name, value, domain=domain, path="/")


def _seed_account_cookies(client: ChatGPTClient, account: Any) -> None:
    raw_cookies = str(getattr(account, "cookies", "") or "").strip()
    if not raw_cookies:
        return

    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookies)
    except Exception:
        return

    for morsel in cookie.values():
        _set_session_cookie(client, morsel.key, morsel.value)


def _build_cookie_header(client: ChatGPTClient) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in client.session.cookies.jar:
        if item.name in seen:
            continue
        seen.add(item.name)
        parts.append(f"{item.name}={item.value}")
    return "; ".join(parts)


def _prepare_auth_context(client: ChatGPTClient, account: Any) -> None:
    session_token = str(getattr(account, "session_token", "") or "").strip()
    if session_token:
        _set_session_cookie(client, "__Secure-next-auth.session-token", session_token)

    _seed_account_cookies(client, account)

    try:
        client.session.get(
            f"{client.BASE}/api/auth/csrf",
            headers=client._headers(
                f"{client.BASE}/api/auth/csrf",
                accept="application/json",
                referer=f"{client.BASE}/",
                fetch_site="same-origin",
            ),
            timeout=15,
        )
    except Exception:
        # CSRF 不是硬性依赖，失败时继续后续测试
        pass


def _ensure_access_token(account: Any, proxy: str) -> tuple[str, str, str]:
    access_token = str(getattr(account, "access_token", "") or "").strip()

    manager = TokenRefreshManager(proxy_url=proxy)
    if access_token:
        valid, _ = manager.validate_token(access_token)
        if valid:
            return access_token, "", ""

    result = manager.refresh_account(account)
    if result.success and result.access_token:
        return result.access_token, result.access_token, result.refresh_token

    error_message = result.error_message or "账号没有可用 access_token，且刷新失败"
    raise RuntimeError(error_message)


def _extract_oai_sc(response: Any) -> str:
    try:
        cookies = getattr(response, "cookies", None)
        if cookies:
            value = cookies.get("oai-sc")
            if value:
                return str(value)
    except Exception:
        pass

    raw_set_cookie = str(response.headers.get("set-cookie") or "")
    match = re.search(r"(?:^|[,;]\s*)oai-sc=([^;,\s]+)", raw_set_cookie)
    return match.group(1).strip() if match else ""


def _get_chat_requirements(client: ChatGPTClient, access_token: str) -> tuple[str, str]:
    generator = SentinelTokenGenerator(device_id=client.device_id, user_agent=client.ua)
    payload = {"p": generator.generate_requirements_token()}

    endpoints = [
        f"{client.BASE}/backend-api/sentinel/chat-requirements",
        f"{client.BASE}/backend-api/sentinel/chat-requirements/prepare",
    ]

    last_error = "获取 chat-requirements 失败"
    for url in endpoints:
        headers = client._headers(
            url,
            accept="*/*",
            referer=f"{client.BASE}/",
            origin=client.BASE,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "authorization": f"Bearer {access_token}",
                "oai-device-id": client.device_id,
                "oai-language": "en-US",
                "cookie": _build_cookie_header(client),
            },
        )

        try:
            response = client.session.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code >= 400:
                last_error = f"chat-requirements HTTP {response.status_code}"
                continue

            data = response.json()
            token = str(data.get("token") or "").strip()
            pow_data = data.get("proofofwork") if isinstance(data, dict) else {}
            if not token or not isinstance(pow_data, dict):
                last_error = "chat-requirements 返回内容缺失"
                continue

            if pow_data.get("required") and pow_data.get("seed"):
                proof_token = generator.generate_token(
                    seed=str(pow_data.get("seed") or ""),
                    difficulty=str(pow_data.get("difficulty") or "0"),
                )
            else:
                proof_token = generator.generate_requirements_token()

            oai_sc = _extract_oai_sc(response)
            if oai_sc:
                _set_session_cookie(client, "oai-sc", oai_sc)

            return token, proof_token
        except Exception as e:
            last_error = str(e) or last_error

    raise RuntimeError(last_error)


def _extract_response_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                texts = [str(part) for part in parts if isinstance(part, str)]
                if texts:
                    return "\n".join(texts).strip()

    value = data.get("v")
    if isinstance(value, str):
        return value.strip()
    return ""


def _extract_response_message_id(data: dict[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, dict):
        message_id = str(message.get("id") or "").strip()
        if message_id:
            return message_id

    operations = data.get("v")
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            message_id = str(
                operation.get("message_id")
                or operation.get("id")
                or ""
            ).strip()
            if message_id:
                return message_id
    return ""


def _merge_response_delta(current_text: str, next_text: str) -> tuple[str, str]:
    current = str(current_text or "")
    candidate = str(next_text or "")
    if not candidate:
        return "", current
    if not current:
        return candidate, candidate
    if candidate.startswith(current):
        return candidate[len(current) :], candidate
    if current.endswith(candidate):
        return "", current
    return candidate, candidate


def _consume_sse_payload(
    payload: str,
    response_text: str,
    conversation_id: str,
    response_message_id: str,
) -> tuple[str, str, str, str]:
    try:
        data = json.loads(payload)
    except Exception:
        return "", response_text, conversation_id, response_message_id

    next_conversation_id = str(data.get("conversation_id") or "").strip() or conversation_id
    next_message_id = _extract_response_message_id(data) or response_message_id

    delta_chunks: list[str] = []
    operations = data.get("v")
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            if not next_conversation_id:
                next_conversation_id = str(operation.get("conversation_id") or "").strip()
            operation_message_id = str(
                operation.get("message_id")
                or operation.get("id")
                or ""
            ).strip()
            if operation_message_id:
                next_message_id = operation_message_id
            if operation.get("o") == "append" and operation.get("p") == "/message/content/parts/0":
                delta_chunks.append(str(operation.get("v") or ""))

    if delta_chunks:
        delta = "".join(delta_chunks)
        return delta, f"{response_text}{delta}", next_conversation_id, next_message_id

    full_text = _extract_response_text(data)
    delta, merged = _merge_response_delta(response_text, full_text)
    return delta, merged, next_conversation_id, next_message_id


def _parse_sse_response(raw_text: str) -> tuple[str, str, str]:
    response_text = ""
    conversation_id = ""
    response_message_id = ""

    for raw_line in str(raw_text or "").splitlines():
        if not raw_line.startswith("data:"):
            continue
        payload = raw_line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        _, response_text, conversation_id, response_message_id = _consume_sse_payload(
            payload,
            response_text,
            conversation_id,
            response_message_id,
        )

    return response_text.strip(), conversation_id, response_message_id


def _normalize_official_conversation_url(client: ChatGPTClient, target_url: str = "") -> str:
    raw = str(target_url or "").strip()
    if not raw:
        return f"{client.BASE}/backend-api/conversation"

    normalized = raw
    if "://" not in normalized:
        normalized = urljoin(f"{client.BASE}/", normalized.lstrip("/"))

    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return f"{client.BASE}/backend-api/conversation"

    path = str(parts.path or "").rstrip("/")
    if not path:
        path = "/backend-api/conversation"
    elif path.endswith("/backend-api"):
        path = f"{path}/conversation"

    return urlunsplit(parts._replace(path=path))


def _normalize_official_models_url(client: ChatGPTClient, target_url: str = "") -> str:
    raw = str(target_url or "").strip()
    if not raw:
        return f"{client.BASE}/backend-api/models"

    normalized = raw
    if "://" not in normalized:
        normalized = urljoin(f"{client.BASE}/", normalized.lstrip("/"))

    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return f"{client.BASE}/backend-api/models"

    path = str(parts.path or "").rstrip("/")
    if not path:
        path = "/backend-api/models"
    elif path.endswith("/models"):
        pass
    elif path.endswith("/conversation"):
        path = f"{path.rsplit('/', 1)[0]}/models"
    elif path.endswith("/backend-api"):
        path = f"{path}/models"
    else:
        path = "/backend-api/models"

    return urlunsplit(parts._replace(path=path))


def _official_target_origin(target_url: str, fallback: str) -> str:
    parts = urlsplit(str(target_url or "").strip())
    if parts.scheme and parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return str(fallback or "").rstrip("/")


def _archive_conversation(client: ChatGPTClient, access_token: str, conversation_id: str) -> None:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return

    url = f"{client.BASE}/backend-api/conversation/{conversation_id}"
    headers = client._headers(
        url,
        accept="application/json",
        referer=f"{client.BASE}/c/{conversation_id}",
        origin=client.BASE,
        content_type="application/json",
        fetch_site="same-origin",
        extra_headers={
            "authorization": f"Bearer {access_token}",
            "oai-device-id": client.device_id,
            "oai-language": "en-US",
            "cookie": _build_cookie_header(client),
        },
    )

    try:
        client.session.patch(url, json={"is_visible": False}, headers=headers, timeout=15)
    except Exception:
        pass


def _build_conversation_headers(
    client: ChatGPTClient,
    access_token: str,
    requirements_token: str,
    proof_token: str,
    target_url: str = "",
) -> tuple[str, dict[str, str]]:
    url = _normalize_official_conversation_url(client, target_url)
    origin = _official_target_origin(url, client.BASE)
    headers = client._headers(
        url,
        accept="text/event-stream",
        referer=f"{origin}/",
        origin=origin,
        content_type="application/json",
        fetch_site="same-origin",
        extra_headers={
            "authorization": f"Bearer {access_token}",
            "oai-device-id": client.device_id,
            "oai-language": "en-US",
            "openai-sentinel-chat-requirements-token": requirements_token,
            "openai-sentinel-proof-token": proof_token,
            "cookie": _build_cookie_header(client),
        },
    )
    return url, headers


def _build_models_headers(
    client: ChatGPTClient,
    access_token: str,
    target_url: str = "",
) -> tuple[str, dict[str, str]]:
    url = _normalize_official_models_url(client, target_url)
    origin = _official_target_origin(url, client.BASE)
    headers = client._headers(
        url,
        accept="application/json, text/plain, */*",
        referer=f"{origin}/",
        origin=origin,
        fetch_site="same-origin",
        extra_headers={
            "authorization": f"Bearer {access_token}",
            "oai-device-id": client.device_id,
            "oai-language": "en-US",
            "cookie": _build_cookie_header(client),
        },
    )
    return url, headers


def _build_quota_headers(
    client: ChatGPTClient,
    access_token: str,
    url: str,
) -> dict[str, str]:
    origin = _official_target_origin(url, client.BASE)
    return client._headers(
        url,
        accept="application/json, text/plain, */*",
        referer=f"{origin}/",
        origin=origin,
        fetch_site="same-origin",
        extra_headers={
            "authorization": f"Bearer {access_token}",
            "oai-device-id": client.device_id,
            "oai-language": "en-US",
            "cookie": _build_cookie_header(client),
        },
    )


def _build_conversation_payload(
    prompt: str,
    *,
    model: str = "auto",
    conversation_id: str = "",
    parent_message_id: str = "",
    history_and_training_disabled: bool = False,
) -> dict[str, Any]:
    payload = {
        "action": "next",
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": [prompt],
                },
                "metadata": {},
            }
        ],
        "parent_message_id": str(parent_message_id or "").strip() or str(uuid.uuid4()),
        "model": str(model or "auto").strip() or "auto",
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "suggestions": [],
        "history_and_training_disabled": bool(history_and_training_disabled),
        "conversation_mode": {"kind": "primary_assistant"},
        "supports_buffering": True,
    }
    normalized_conversation_id = str(conversation_id or "").strip()
    if normalized_conversation_id:
        payload["conversation_id"] = normalized_conversation_id
    return payload


def _prepare_chat_request(
    account: Any,
    proxy: str,
    prompt: str,
    *,
    model: str = "auto",
    conversation_id: str = "",
    parent_message_id: str = "",
    target_url: str = "",
    history_and_training_disabled: bool = False,
    chain_name: str,
) -> PreparedChatRequest:
    normalized_model = str(model or "auto").strip() or "auto"
    normalized_conversation_id = str(conversation_id or "").strip()
    normalized_parent_message_id = str(parent_message_id or "").strip()
    client = ChatGPTClient(proxy=proxy, verbose=False)

    logger.info(
        "[chatgpt-message] chain=%s start shared_test_flow=true proxy=%s model=%s conversation_id=%s parent_message_id=%s history_disabled=%s prompt_len=%s",
        chain_name,
        _sanitize_proxy(proxy),
        normalized_model,
        normalized_conversation_id,
        normalized_parent_message_id,
        bool(history_and_training_disabled),
        len(str(prompt or "")),
    )

    _prepare_auth_context(client, account)
    logger.info("[chatgpt-message] chain=%s step=prepare_auth_context ok", chain_name)

    access_token, updated_access_token, updated_refresh_token = _ensure_access_token(account, proxy)
    logger.info(
        "[chatgpt-message] chain=%s step=ensure_access_token ok refreshed=%s",
        chain_name,
        bool(updated_access_token),
    )

    requirements_token, proof_token = _get_chat_requirements(client, access_token)
    logger.info("[chatgpt-message] chain=%s step=get_chat_requirements ok", chain_name)

    url, headers = _build_conversation_headers(
        client,
        access_token,
        requirements_token,
        proof_token,
        target_url=target_url,
    )
    payload = _build_conversation_payload(
        prompt,
        model=normalized_model,
        conversation_id=normalized_conversation_id,
        parent_message_id=normalized_parent_message_id,
        history_and_training_disabled=history_and_training_disabled,
    )
    logger.info(
        "[chatgpt-message] chain=%s step=build_conversation_request ok url=%s has_conversation_id=%s",
        chain_name,
        url,
        "conversation_id" in payload,
    )

    return PreparedChatRequest(
        client=client,
        access_token=access_token,
        updated_access_token=updated_access_token,
        updated_refresh_token=updated_refresh_token,
        url=url,
        headers=headers,
        payload=payload,
    )


def _normalize_model_entry(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None

    identifier = str(item.get("slug") or item.get("id") or item.get("name") or "").strip()
    if not identifier:
        return None

    if "slug" not in item and not any(
        key in item for key in ("title", "description", "max_tokens", "enabled_tools", "tags", "name")
    ):
        return None

    title = str(item.get("title") or item.get("name") or item.get("slug") or item.get("id") or "").strip()
    title = title or identifier

    description_parts: list[str] = []
    description = str(item.get("description") or "").strip()
    if description:
        description_parts.append(description)

    owned_by = str(item.get("owned_by") or "").strip()
    if owned_by:
        description_parts.append(f"owned_by: {owned_by}")

    max_tokens = item.get("max_tokens")
    if max_tokens in (None, ""):
        max_tokens = item.get("max_tokens_total")
    if max_tokens not in (None, ""):
        description_parts.append(f"max_tokens: {max_tokens}")

    return {
        "id": identifier,
        "title": title,
        "description": " | ".join(description_parts),
    }


def _extract_model_entries(payload: Any) -> list[dict[str, str]]:
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates.extend(payload)
    elif isinstance(payload, dict):
        for key in ("models", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        categories = payload.get("categories")
        if isinstance(categories, list):
            for category in categories:
                if not isinstance(category, dict):
                    continue
                models = category.get("models")
                if isinstance(models, list):
                    candidates.extend(models)

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        normalized = _normalize_model_entry(item)
        if not normalized:
            continue
        identifier = normalized["id"]
        if identifier in seen:
            continue
        seen.add(identifier)
        results.append(normalized)
    return results


def _build_quota_candidate_urls(client: ChatGPTClient, target_url: str = "") -> list[str]:
    origin = _official_target_origin(str(target_url or "").strip(), client.BASE).rstrip("/")
    candidates = [
        f"{origin}/backend-api/accounts/check/v4-2023-04-27",
        f"{origin}/backend-api/accounts/check",
        f"{origin}/backend-api/me",
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _summary_value(data: Any, path: str) -> Any:
    current = data
    for segment in str(path or "").split("."):
        if not segment:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _clean_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if value not in (None, "", [], {})
    }


def _extract_quota_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    summary = {
        "account_id": _summary_value(payload, "accounts.default.account.account_id")
        or payload.get("account_id")
        or payload.get("id"),
        "account_structure": _summary_value(payload, "accounts.default.account.structure"),
        "subscription_plan": _summary_value(payload, "accounts.default.entitlement.subscription_plan")
        or payload.get("plan_type"),
        "has_active_subscription": _summary_value(payload, "accounts.default.entitlement.has_active_subscription"),
        "expires_at": _summary_value(payload, "accounts.default.entitlement.expires_at"),
        "will_renew": _summary_value(payload, "accounts.default.last_active_subscription.will_renew"),
        "purchase_origin_platform": _summary_value(payload, "accounts.default.last_active_subscription.purchase_origin_platform"),
        "has_previously_paid_subscription": _summary_value(
            payload,
            "accounts.default.account.has_previously_paid_subscription",
        ),
        "plan_type": payload.get("plan_type"),
    }
    return _clean_summary(summary)


def _is_signal_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    keywords = (
        "remaining",
        "quota",
        "limit",
        "cap",
        "usage",
        "reset",
        "expire",
        "renew",
        "balance",
        "credit",
    )
    return any(keyword in normalized for keyword in keywords)


def _append_signal(signals: list[dict[str, Any]], seen: set[str], path: str, value: Any) -> None:
    normalized_path = str(path or "").strip()
    if not normalized_path or normalized_path in seen:
        return
    if isinstance(value, (dict, list)):
        return
    seen.add(normalized_path)
    signals.append({"path": normalized_path, "value": value})


def _collect_quota_signals(payload: Any) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any, path: str = "", depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                next_path = f"{path}.{key}" if path else str(key)
                if _is_signal_key(key):
                    _append_signal(signals, seen, next_path, item)
                walk(item, next_path, depth + 1)
            return
        if isinstance(value, list):
            for index, item in enumerate(value[:12]):
                walk(item, f"{path}[{index}]", depth + 1)

    walk(payload)
    return signals[:32]


def _result_from_http_error(
    *,
    status_code: int,
    proxy: str,
    updated_access_token: str,
    updated_refresh_token: str,
    response_text: str = "",
) -> ChatGPTMessageTestResult:
    normalized_response_text = str(response_text or "")
    if status_code == 404 and (
        "history_disabled_conversation_not_found" in normalized_response_text
        or "Conversation not found" in normalized_response_text
    ):
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=False,
            message="当前会话上下文不存在，请新建会话后重试",
            response_text=normalized_response_text,
            response_excerpt=normalized_response_text[:200],
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    if status_code == 401:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=True,
            message="会话无效或 access_token 已过期",
            response_text=normalized_response_text,
            response_excerpt=normalized_response_text[:200],
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    if status_code == 403:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=True,
            message="账号被拒绝访问或已受限",
            response_text=normalized_response_text,
            response_excerpt=normalized_response_text[:200],
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    return ChatGPTMessageTestResult(
        ok=False,
        invalid=False,
        message=f"发消息失败: HTTP {status_code}",
        response_text=normalized_response_text,
        response_excerpt=normalized_response_text[:200],
        used_proxy=proxy,
        updated_access_token=updated_access_token,
        updated_refresh_token=updated_refresh_token,
    )


def send_chat_message(
    account: Any,
    proxy: str,
    prompt: str,
    *,
    model: str = "auto",
    conversation_id: str = "",
    parent_message_id: str = "",
    target_url: str = "",
    history_and_training_disabled: bool = False,
    archive_after_send: bool = False,
) -> ChatGPTMessageTestResult:
    chain_name = "send_chat_message"
    client = None
    response = None
    try:
        prepared = _prepare_chat_request(
            account,
            proxy,
            prompt,
            model=model,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            target_url=target_url,
            history_and_training_disabled=history_and_training_disabled,
            chain_name=chain_name,
        )
        client = prepared.client

        response = client.session.post(
            prepared.url,
            json=prepared.payload,
            headers=prepared.headers,
            timeout=90,
        )
        _log_http_response(chain_name, response, note="response_received")

        if response.status_code >= 400:
            response_text = str(response.text or "")
            _log_http_response(
                chain_name,
                response,
                note="http_error",
                body_excerpt=_truncate_text(response_text, limit=600),
            )
            return _result_from_http_error(
                status_code=response.status_code,
                proxy=proxy,
                updated_access_token=prepared.updated_access_token,
                updated_refresh_token=prepared.updated_refresh_token,
                response_text=response_text,
            )

        raw_text = str(response.text or "")
        response_text, response_conversation_id, response_message_id = _parse_sse_response(raw_text)
        if archive_after_send:
            _archive_conversation(client, prepared.access_token, response_conversation_id)

        if not response_text:
            logger.warning(
                "[chatgpt-message] chain=%s note=empty_sse_response body_excerpt=%s",
                chain_name,
                _truncate_text(raw_text, limit=600),
            )
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=False,
                message="已发送测试消息，但未解析到有效回复",
                conversation_id=response_conversation_id,
                response_message_id=response_message_id,
                used_proxy=proxy,
                updated_access_token=prepared.updated_access_token,
                updated_refresh_token=prepared.updated_refresh_token,
            )

        logger.info(
            "[chatgpt-message] chain=%s note=done conversation_id=%s response_message_id=%s response_excerpt=%s",
            chain_name,
            response_conversation_id,
            response_message_id,
            _truncate_text(response_text, limit=160),
        )
        return ChatGPTMessageTestResult(
            ok=True,
            invalid=False,
            message=f"通过代理成功发送消息，回复: {response_text[:80]}",
            response_excerpt=response_text[:200],
            response_text=response_text,
            model=str(model or "auto").strip() or "auto",
            conversation_id=response_conversation_id,
            response_message_id=response_message_id,
            used_proxy=proxy,
            updated_access_token=prepared.updated_access_token,
            updated_refresh_token=prepared.updated_refresh_token,
        )
    except Exception as e:
        logger.exception(
            "[chatgpt-message] chain=%s note=exception proxy=%s",
            chain_name,
            _sanitize_proxy(proxy),
        )
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=False,
            message=_exception_message(e, "ChatGPT 发消息测试失败"),
            used_proxy=proxy,
        )
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.session.close()
        except Exception:
            pass


def fetch_available_models(
    account: Any,
    proxy: str,
    *,
    target_url: str = "",
) -> ChatGPTModelsResult:
    chain_name = "fetch_available_models"
    client = None
    response = None
    try:
        client = ChatGPTClient(proxy=proxy, verbose=False)
        _prepare_auth_context(client, account)

        access_token, updated_access_token, updated_refresh_token = _ensure_access_token(account, proxy)
        url, headers = _build_models_headers(client, access_token, target_url=target_url)

        response = client.session.get(
            url,
            headers=headers,
            timeout=60,
        )
        _log_http_response(chain_name, response, note="response_received")

        raw_text = str(getattr(response, "text", "") or "")
        if response.status_code >= 400:
            _log_http_response(
                chain_name,
                response,
                note="http_error",
                body_excerpt=_truncate_text(raw_text, limit=600),
            )
            if response.status_code == 401:
                message = "会话无效或 access_token 已过期"
                invalid = True
            elif response.status_code == 403:
                message = "账号被拒绝访问或已受限"
                invalid = True
            else:
                message = f"查询模型失败: HTTP {response.status_code}"
                invalid = False

            body_excerpt = _truncate_text(raw_text, limit=200)
            if body_excerpt:
                message = f"{message} - {body_excerpt}"

            return ChatGPTModelsResult(
                ok=False,
                invalid=invalid,
                message=message,
                used_proxy=proxy,
                models_url=url,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
                response_status_code=response.status_code,
            )

        try:
            data = response.json()
        except Exception:
            data = raw_text

        models = _extract_model_entries(data)
        message = (
            f"已获取 {len(models)} 个模型"
            if models
            else "已获取模型响应，但未解析出标准模型列表"
        )
        return ChatGPTModelsResult(
            ok=True,
            invalid=False,
            message=message,
            used_proxy=proxy,
            models_url=url,
            models=models,
            data=data,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
            response_status_code=response.status_code,
        )
    except Exception as e:
        logger.exception(
            "[chatgpt-message] chain=%s note=exception proxy=%s",
            chain_name,
            _sanitize_proxy(proxy),
        )
        return ChatGPTModelsResult(
            ok=False,
            invalid=False,
            message=_exception_message(e, "查询 ChatGPT 模型列表失败"),
            used_proxy=proxy,
        )
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.session.close()
        except Exception:
            pass


def fetch_official_quota(
    account: Any,
    proxy: str,
    *,
    target_url: str = "",
) -> ChatGPTQuotaResult:
    chain_name = "fetch_official_quota"
    client = None
    response = None
    last_message = "查询 ChatGPT 官方配额失败"
    last_status_code = 0
    last_query_url = ""

    try:
        client = ChatGPTClient(proxy=proxy, verbose=False)
        _prepare_auth_context(client, account)

        access_token, updated_access_token, updated_refresh_token = _ensure_access_token(account, proxy)

        for url in _build_quota_candidate_urls(client, target_url):
            last_query_url = url
            try:
                headers = _build_quota_headers(client, access_token, url)
                response = client.session.get(
                    url,
                    headers=headers,
                    timeout=60,
                )
                _log_http_response(chain_name, response, note=f"response_received url={url}")

                raw_text = str(getattr(response, "text", "") or "")
                last_status_code = int(getattr(response, "status_code", 0) or 0)
                if response.status_code >= 400:
                    _log_http_response(
                        chain_name,
                        response,
                        note=f"http_error url={url}",
                        body_excerpt=_truncate_text(raw_text, limit=600),
                    )
                    if response.status_code == 401:
                        return ChatGPTQuotaResult(
                            ok=False,
                            invalid=True,
                            message="会话无效或 access_token 已过期",
                            used_proxy=proxy,
                            query_url=url,
                            updated_access_token=updated_access_token,
                            updated_refresh_token=updated_refresh_token,
                            response_status_code=response.status_code,
                        )
                    if response.status_code == 403:
                        return ChatGPTQuotaResult(
                            ok=False,
                            invalid=True,
                            message="账号被拒绝访问或已受限",
                            used_proxy=proxy,
                            query_url=url,
                            updated_access_token=updated_access_token,
                            updated_refresh_token=updated_refresh_token,
                            response_status_code=response.status_code,
                        )

                    body_excerpt = _truncate_text(raw_text, limit=200)
                    last_message = f"查询官方配额失败: HTTP {response.status_code}"
                    if body_excerpt:
                        last_message = f"{last_message} - {body_excerpt}"
                    if response.status_code == 404:
                        continue
                    continue

                try:
                    data = response.json()
                except Exception:
                    data = raw_text

                summary = _extract_quota_summary(data)
                signals = _collect_quota_signals(data)
                plan = str(summary.get("subscription_plan") or summary.get("plan_type") or "").strip()
                if plan:
                    message = f"已获取官方账户信息，当前套餐: {plan}"
                else:
                    message = "已获取官方账户信息"
                if signals:
                    message = f"{message}，识别到 {len(signals)} 个配额相关字段"

                return ChatGPTQuotaResult(
                    ok=True,
                    invalid=False,
                    message=message,
                    used_proxy=proxy,
                    query_url=url,
                    summary=summary,
                    signals=signals,
                    data=data,
                    updated_access_token=updated_access_token,
                    updated_refresh_token=updated_refresh_token,
                    response_status_code=response.status_code,
                )
            except Exception as e:
                logger.exception(
                    "[chatgpt-message] chain=%s note=exception proxy=%s url=%s",
                    chain_name,
                    _sanitize_proxy(proxy),
                    url,
                )
                last_message = str(e) or last_message
            finally:
                try:
                    if response is not None:
                        response.close()
                except Exception:
                    pass
                response = None

        return ChatGPTQuotaResult(
            ok=False,
            invalid=False,
            message=last_message,
            used_proxy=proxy,
            query_url=last_query_url,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
            response_status_code=last_status_code,
        )
    except Exception as e:
        logger.exception(
            "[chatgpt-message] chain=%s note=exception proxy=%s",
            chain_name,
            _sanitize_proxy(proxy),
        )
        return ChatGPTQuotaResult(
            ok=False,
            invalid=False,
            message=_exception_message(e, "查询 ChatGPT 官方配额失败"),
            used_proxy=proxy,
        )
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.session.close()
        except Exception:
            pass


def send_test_message(account: Any, proxy: str, prompt: str = TEST_PROMPT) -> ChatGPTMessageTestResult:
    return send_chat_message(
        account,
        proxy=proxy,
        prompt=prompt,
        model="auto",
        history_and_training_disabled=True,
        archive_after_send=True,
    )


def stream_chat_message(
    account: Any,
    proxy: str,
    prompt: str,
    *,
    model: str = "auto",
    conversation_id: str = "",
    parent_message_id: str = "",
    target_url: str = "",
    history_and_training_disabled: bool = False,
) -> Iterator[dict[str, Any]]:
    chain_name = "stream_chat_message"
    client = None
    response = None
    try:
        prepared = _prepare_chat_request(
            account,
            proxy,
            prompt,
            model=model,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            target_url=target_url,
            history_and_training_disabled=history_and_training_disabled,
            chain_name=chain_name,
        )
        client = prepared.client

        yield {
            "event": "meta",
            "data": {
                "used_proxy": proxy,
                "model": str(model or "auto").strip() or "auto",
                "conversation_id": str(conversation_id or "").strip(),
                "parent_message_id": str(parent_message_id or "").strip(),
                "target_url": prepared.url,
                "chain": chain_name,
                "shared_test_flow": True,
                "request_mode": "stream",
            },
        }

        with _open_streaming_response(
            client.session,
            "POST",
            prepared.url,
            json=prepared.payload,
            headers=prepared.headers,
            timeout=180,
        ) as stream_response:
            response = stream_response
            _log_http_response(chain_name, response, note="stream_opened")
            if response.status_code >= 400:
                response_text = str(response.text or "")
                response_headers = _response_header_snapshot(response)
                _log_http_response(
                    chain_name,
                    response,
                    note="http_error",
                    body_excerpt=_truncate_text(response_text, limit=600),
                )
                result = _result_from_http_error(
                    status_code=response.status_code,
                    proxy=proxy,
                    updated_access_token=prepared.updated_access_token,
                    updated_refresh_token=prepared.updated_refresh_token,
                    response_text=response_text,
                )
                yield {
                    "event": "error",
                    "data": {
                        "ok": result.ok,
                        "invalid": result.invalid,
                        "message": result.message,
                        "response_excerpt": result.response_excerpt,
                        "response_text": result.response_text,
                        "conversation_id": result.conversation_id,
                        "response_message_id": result.response_message_id,
                        "used_proxy": result.used_proxy,
                        "updated_access_token": result.updated_access_token,
                        "updated_refresh_token": result.updated_refresh_token,
                        "target_url": prepared.url,
                        "response_status_code": response.status_code,
                        "response_headers": response_headers,
                        "chain": chain_name,
                        "shared_test_flow": True,
                    },
                }
                return

            response_text = ""
            response_conversation_id = str(conversation_id or "").strip()
            response_message_id = ""
            raw_line_samples: list[str] = []
            payload_samples: list[str] = []

            for raw_line in _iter_stream_lines(response):
                raw_line_text = str(raw_line or "")
                if raw_line_text and len(raw_line_samples) < 8:
                    raw_line_samples.append(_truncate_text(raw_line_text, limit=240))

                line = raw_line_text.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if not payload_text:
                    continue
                if len(payload_samples) < 8:
                    payload_samples.append(_truncate_text(payload_text, limit=240))
                if payload_text == "[DONE]":
                    break

                delta, response_text, response_conversation_id, response_message_id = _consume_sse_payload(
                    payload_text,
                    response_text,
                    response_conversation_id,
                    response_message_id,
                )
                if delta:
                    yield {
                        "event": "delta",
                        "data": {
                            "delta": delta,
                            "conversation_id": response_conversation_id,
                            "response_message_id": response_message_id,
                            "used_proxy": proxy,
                            "model": str(model or "auto").strip() or "auto",
                        },
                    }

            if not response_text:
                logger.warning(
                    "[chatgpt-message] chain=%s note=empty_stream_response raw_lines=%s payloads=%s",
                    chain_name,
                    raw_line_samples,
                    payload_samples,
                )
                yield {
                    "event": "error",
                    "data": {
                        "ok": False,
                        "invalid": False,
                        "message": "已发送消息，但未解析到有效回复",
                        "response_excerpt": "",
                        "response_text": "",
                        "conversation_id": response_conversation_id,
                        "response_message_id": response_message_id,
                        "used_proxy": proxy,
                        "model": str(model or "auto").strip() or "auto",
                        "updated_access_token": prepared.updated_access_token,
                        "updated_refresh_token": prepared.updated_refresh_token,
                        "target_url": prepared.url,
                        "debug_raw_lines": raw_line_samples,
                        "debug_payloads": payload_samples,
                        "response_headers": _response_header_snapshot(response),
                        "chain": chain_name,
                        "shared_test_flow": True,
                    },
                }
                return

            logger.info(
                "[chatgpt-message] chain=%s note=done conversation_id=%s response_message_id=%s response_excerpt=%s",
                chain_name,
                response_conversation_id,
                response_message_id,
                _truncate_text(response_text, limit=160),
            )
            yield {
                "event": "done",
                "data": {
                    "ok": True,
                    "invalid": False,
                    "message": f"通过代理成功发送消息，回复: {response_text[:80]}",
                    "response_excerpt": response_text[:200],
                    "response_text": response_text,
                    "conversation_id": response_conversation_id,
                    "response_message_id": response_message_id,
                    "used_proxy": proxy,
                    "model": str(model or "auto").strip() or "auto",
                    "updated_access_token": prepared.updated_access_token,
                    "updated_refresh_token": prepared.updated_refresh_token,
                    "target_url": prepared.url,
                    "chain": chain_name,
                    "shared_test_flow": True,
                },
            }
    except Exception as e:
        logger.exception(
            "[chatgpt-message] chain=%s note=exception proxy=%s",
            chain_name,
            _sanitize_proxy(proxy),
        )
        yield {
            "event": "error",
            "data": {
                "ok": False,
                "invalid": False,
                "message": _exception_message(e, "ChatGPT 发消息测试失败"),
                "used_proxy": proxy,
                "model": str(model or "auto").strip() or "auto",
                "target_url": str(target_url or "").strip(),
                "chain": chain_name,
                "shared_test_flow": True,
            },
        }
    finally:
        try:
            if client is not None:
                client.session.close()
        except Exception:
            pass
