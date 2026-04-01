from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from .chatgpt_client import ChatGPTClient
from .sentinel_token import SentinelTokenGenerator
from .token_refresh import TokenRefreshManager


TEST_PROMPT = "Reply with exactly TEST_OK"
TEST_PROMPT = "Reply with exactly haogege"


@dataclass
class ChatGPTMessageTestResult:
    ok: bool
    invalid: bool
    message: str
    response_excerpt: str = ""
    model: str = "auto"
    conversation_id: str = ""
    used_proxy: str = ""
    updated_access_token: str = ""
    updated_refresh_token: str = ""


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


def _parse_sse_response(raw_text: str) -> tuple[str, str]:
    response_text = ""
    conversation_id = ""

    for raw_line in str(raw_text or "").splitlines():
        if not raw_line.startswith("data:"):
            continue
        payload = raw_line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        try:
            data = json.loads(payload)
        except Exception:
            continue

        if not conversation_id:
            conversation_id = str(data.get("conversation_id") or "").strip()

        text = _extract_response_text(data)
        if text:
            if data.get("o") == "append":
                response_text += text
            else:
                response_text = text

        operations = data.get("v")
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                if not conversation_id:
                    conversation_id = str(operation.get("conversation_id") or "").strip()
                if operation.get("o") == "append" and operation.get("p") == "/message/content/parts/0":
                    response_text += str(operation.get("v") or "")

    return response_text.strip(), conversation_id


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


def send_test_message(account: Any, proxy: str, prompt: str = TEST_PROMPT) -> ChatGPTMessageTestResult:
    if not proxy:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=False,
            message="未配置可用代理，无法执行 ChatGPT 发消息测试",
        )

    client = ChatGPTClient(proxy=proxy, verbose=False)
    try:
        _prepare_auth_context(client, account)
        access_token, updated_access_token, updated_refresh_token = _ensure_access_token(account, proxy)
        requirements_token, proof_token = _get_chat_requirements(client, access_token)

        url = f"{client.BASE}/backend-api/conversation"
        headers = client._headers(
            url,
            accept="text/event-stream",
            referer=f"{client.BASE}/",
            origin=client.BASE,
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
            "parent_message_id": "client-created-root",
            "model": "auto",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "suggestions": [],
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
            "supports_buffering": True,
        }

        response = client.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=90,
        )

        if response.status_code == 401:
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=True,
                message="会话无效或 access_token 已过期",
                used_proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
            )
        if response.status_code == 403:
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=True,
                message="账号被拒绝访问或已受限",
                used_proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
            )
        if response.status_code >= 400:
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=False,
                message=f"发消息失败: HTTP {response.status_code}",
                response_excerpt=response.text[:200],
                used_proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
            )

        response_text, conversation_id = _parse_sse_response(response.text)
        _archive_conversation(client, access_token, conversation_id)

        if not response_text:
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=False,
                message="已发送测试消息，但未解析到有效回复",
                conversation_id=conversation_id,
                used_proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
            )

        return ChatGPTMessageTestResult(
            ok=True,
            invalid=False,
            message=f"通过代理成功发送测试消息，回复: {response_text[:80]}",
            response_excerpt=response_text[:200],
            conversation_id=conversation_id,
            used_proxy=proxy,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
        )
    except Exception as e:
        return ChatGPTMessageTestResult(
            ok=False,
            invalid=False,
            message=str(e) or "ChatGPT 发消息测试失败",
            used_proxy=proxy,
        )
    finally:
        try:
            client.session.close()
        except Exception:
            pass
