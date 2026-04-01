from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Iterator

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
    response_text: str = ""
    model: str = "auto"
    conversation_id: str = ""
    response_message_id: str = ""
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
) -> tuple[str, dict[str, str]]:
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
    archive_after_send: bool = False,
) -> ChatGPTMessageTestResult:
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
        url, headers = _build_conversation_headers(client, access_token, requirements_token, proof_token)
        payload = _build_conversation_payload(
            prompt,
            model=model,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
        )

        response = client.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=90,
        )

        if response.status_code >= 400:
            return _result_from_http_error(
                status_code=response.status_code,
                proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
                response_text=response.text or "",
            )

        response_text, response_conversation_id, response_message_id = _parse_sse_response(response.text)
        if archive_after_send:
            _archive_conversation(client, access_token, response_conversation_id)

        if not response_text:
            return ChatGPTMessageTestResult(
                ok=False,
                invalid=False,
                message="已发送测试消息，但未解析到有效回复",
                conversation_id=response_conversation_id,
                response_message_id=response_message_id,
                used_proxy=proxy,
                updated_access_token=updated_access_token,
                updated_refresh_token=updated_refresh_token,
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

    client = ChatGPTClient(proxy=proxy, verbose=False)
    try:
        _prepare_auth_context(client, account)
        access_token, updated_access_token, updated_refresh_token = _ensure_access_token(account, proxy)
        requirements_token, proof_token = _get_chat_requirements(client, access_token)
        url, headers = _build_conversation_headers(client, access_token, requirements_token, proof_token)
        payload = _build_conversation_payload(
            prompt,
            model=model,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
        )
        response = None

        yield {
            "event": "meta",
            "data": {
                "used_proxy": proxy,
                "model": str(model or "auto").strip() or "auto",
                "conversation_id": str(conversation_id or "").strip(),
                "parent_message_id": str(parent_message_id or "").strip(),
            },
        }

        response = client.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=180,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                result = _result_from_http_error(
                    status_code=response.status_code,
                    proxy=proxy,
                    updated_access_token=updated_access_token,
                    updated_refresh_token=updated_refresh_token,
                    response_text=response.text or "",
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
                    },
                }
                return

            response_text = ""
            response_conversation_id = str(conversation_id or "").strip()
            response_message_id = ""

            for raw_line in response.iter_lines(decode_unicode=True):
                line = str(raw_line or "").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if not payload_text:
                    continue
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
                        "updated_access_token": updated_access_token,
                        "updated_refresh_token": updated_refresh_token,
                    },
                }
                return

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
                    "updated_access_token": updated_access_token,
                        "updated_refresh_token": updated_refresh_token,
                    },
                }
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
    except Exception as e:
        yield {
            "event": "error",
            "data": {
                "ok": False,
                "invalid": False,
                "message": str(e) or "ChatGPT 发消息测试失败",
                "used_proxy": proxy,
                "model": str(model or "auto").strip() or "auto",
            },
        }
    finally:
        try:
            client.session.close()
        except Exception:
            pass
