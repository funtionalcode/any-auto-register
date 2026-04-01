from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
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


def _build_conversation_payload(
    prompt: str,
    *,
    model: str = "auto",
    conversation_id: str = "",
    parent_message_id: str = "",
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
        "history_and_training_disabled": True,
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
    chain_name: str,
) -> PreparedChatRequest:
    normalized_model = str(model or "auto").strip() or "auto"
    normalized_conversation_id = str(conversation_id or "").strip()
    normalized_parent_message_id = str(parent_message_id or "").strip()
    client = ChatGPTClient(proxy=proxy, verbose=False)

    logger.info(
        "[chatgpt-message] chain=%s start shared_test_flow=true proxy=%s model=%s conversation_id=%s parent_message_id=%s prompt_len=%s",
        chain_name,
        _sanitize_proxy(proxy),
        normalized_model,
        normalized_conversation_id,
        normalized_parent_message_id,
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


def _result_from_http_error(
    *,
    status_code: int,
    proxy: str,
    updated_access_token: str,
    updated_refresh_token: str,
    response_text: str = "",
) -> ChatGPTMessageTestResult:
    if status_code == 401:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=True,
            message="会话无效或 access_token 已过期",
            response_text=response_text,
            response_excerpt=response_text[:200],
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    if status_code == 403:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=True,
            message="账号被拒绝访问或已受限",
            response_text=response_text,
            response_excerpt=response_text[:200],
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    return ChatGPTMessageTestResult(
        ok=False,
        invalid=False,
        message=f"发消息失败: HTTP {status_code}",
        response_text=response_text,
        response_excerpt=response_text[:200],
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
    archive_after_send: bool = False,
) -> ChatGPTMessageTestResult:
    if not proxy:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=False,
            message="未配置可用代理，无法执行 ChatGPT 发消息测试",
        )

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
            message=str(e) or "ChatGPT 发消息测试失败",
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
) -> Iterator[dict[str, Any]]:
    if not proxy:
        yield {
            "event": "error",
            "data": {
                "ok": False,
                "invalid": False,
                "message": "未配置可用代理，无法执行 ChatGPT 发消息测试",
                "used_proxy": "",
            },
        }
        return

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

        response = client.session.post(
            prepared.url,
            json=prepared.payload,
            headers=prepared.headers,
            timeout=180,
            stream=True,
        )
        _log_http_response(chain_name, response, note="stream_opened")
        try:
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

            for raw_line in response.iter_lines(decode_unicode=True):
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
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
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
                "message": str(e) or "ChatGPT 发消息测试失败",
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
