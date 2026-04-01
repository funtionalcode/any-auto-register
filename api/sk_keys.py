import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlmodel import Session, select

from core.db import AccountModel, ProxyModel, SKApiKeyModel, SKApiKeyUsageLog, UserModel, engine, get_session
from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url
from core.security import (
    generate_sk_api_key,
    get_current_user,
    hash_sk_api_key,
)

router = APIRouter(tags=["sk"])
openai_router = APIRouter(prefix="/v1", tags=["openai"])
_sk_bearer_scheme = HTTPBearer(auto_error=False)
OFFICIAL_CHATGPT_BASE = "https://chatgpt.com"
OFFICIAL_CHATGPT_CONVERSATION_URL = f"{OFFICIAL_CHATGPT_BASE}/backend-api/conversation"
OFFICIAL_CHATGPT_MODELS_URL = f"{OFFICIAL_CHATGPT_BASE}/backend-api/models"
DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AVAILABLE_CHATGPT_ACCOUNT_STATUSES = ("registered", "trial", "subscribed")
_chatgpt_account_rr_lock = threading.Lock()
_chatgpt_account_rr_index = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_non_negative_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} 必须是整数")
    if normalized < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} 不能小于 0")
    return normalized


def _estimate_text_tokens(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _estimate_chat_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        total += 4
        total += _estimate_text_tokens(item.get("role"))
        total += _estimate_text_tokens(item.get("content"))
        total += _estimate_text_tokens(item.get("name"))
    return total


def _planned_completion_tokens(payload: dict[str, Any]) -> int:
    if "max_completion_tokens" in payload:
        return _normalize_non_negative_int(payload.get("max_completion_tokens", 0), field_name="max_completion_tokens")
    return _normalize_non_negative_int(payload.get("max_tokens", 0), field_name="max_tokens")


def _normalize_chat_url(target_url: str) -> str:
    value = str(target_url or "").strip()
    if not value:
        return OFFICIAL_CHATGPT_CONVERSATION_URL

    if "://" not in value and value.lstrip("/").startswith("backend-api/"):
        value = f"{OFFICIAL_CHATGPT_BASE}/{value.lstrip('/')}"

    parts = urlsplit(value)
    path = str(parts.path or "").rstrip("/")
    host = str(parts.netloc or "").lower()
    if host.endswith("chatgpt.com"):
        if not path or path == "/":
            path = "/backend-api/conversation"
        elif path.endswith("/backend-api"):
            path = f"{path}/conversation"
        elif path.endswith("/backend-api/conversation"):
            pass
        return urlunsplit(parts._replace(path=path))

    if path.endswith("/chat/completions"):
        return urlunsplit(parts._replace(path=path))

    next_path = f"{path}/chat/completions" if path else "/chat/completions"
    return urlunsplit(parts._replace(path=next_path))


def _derive_models_url(target_url: str) -> str:
    normalized = _normalize_chat_url(target_url)
    parts = urlsplit(normalized)
    path = str(parts.path or "")
    if path.endswith("/backend-api/conversation"):
        path = f"{path[:-len('/backend-api/conversation')]}/backend-api/models" if path != "/backend-api/conversation" else "/backend-api/models"
    elif path.endswith("/chat/completions"):
        path = f"{path[:-len('/chat/completions')]}/models"
    else:
        path = "/models"
    return urlunsplit(parts._replace(path=path))


def _is_official_chatgpt_target(target_url: str) -> bool:
    normalized = _normalize_chat_url(target_url)
    parts = urlsplit(normalized)
    host = str(parts.netloc or "").lower()
    path = str(parts.path or "").rstrip("/")
    return host.endswith("chatgpt.com") and path.endswith("/backend-api/conversation")


def _extract_message_text_content(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        text = str(value.get("text") or value.get("content") or "").strip()
        if text:
            return text
        return _extract_message_text_content(value.get("parts"))
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                parts.append(text)
                continue
            if item_type in {"image_url", "input_image", "image"}:
                image_value = item.get("image_url")
                if isinstance(image_value, dict):
                    image_value = image_value.get("url")
                image_text = str(image_value or item.get("url") or "").strip()
                if image_text:
                    parts.append(f"[image: {image_text}]")
        return "\n".join(part for part in parts if part)
    return str(value).strip()


def _openai_messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    normalized_parts: list[str] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower() or "user"
        content = _extract_message_text_content(item.get("content"))
        if not content:
            continue
        name = str(item.get("name") or "").strip()
        label = role.upper() if not name else f"{role.upper()}:{name}"
        normalized_parts.append(f"{label}:\n{content}")

    if not normalized_parts:
        return ""
    if len(normalized_parts) == 1 and normalized_parts[0].startswith("USER:\n"):
        return normalized_parts[0][len("USER:\n") :]
    return "Continue the conversation and reply as the assistant.\n\n" + "\n\n".join(normalized_parts)


def _build_chatgpt_account_from_upstream_auth(upstream_value: str) -> SimpleNamespace:
    raw = str(upstream_value or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="官方 ChatGPT 模式要求配置 Upstream API Key，内容应为 access_token 或包含 access_token 的 JSON",
        )

    access_token = raw
    refresh_token = ""
    session_token = ""
    cookies = ""

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            access_token = str(
                parsed.get("access_token")
                or parsed.get("accessToken")
                or parsed.get("token")
                or ""
            ).strip()
            refresh_token = str(parsed.get("refresh_token") or parsed.get("refreshToken") or "").strip()
            session_token = str(parsed.get("session_token") or parsed.get("sessionToken") or "").strip()
            cookies = str(parsed.get("cookies") or parsed.get("cookie") or "").strip()

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="官方 ChatGPT 模式缺少 access_token",
        )

    return SimpleNamespace(
        access_token=access_token,
        refresh_token=refresh_token,
        session_token=session_token,
        cookies=cookies,
    )


def _persist_chatgpt_upstream_tokens(
    item_id: int,
    original_value: str,
    *,
    updated_access_token: str = "",
    updated_refresh_token: str = "",
) -> None:
    updated_access_token = str(updated_access_token or "").strip()
    updated_refresh_token = str(updated_refresh_token or "").strip()
    if not updated_access_token and not updated_refresh_token:
        return

    original_text = str(original_value or "").strip()
    with Session(engine) as session:
        item = session.get(SKApiKeyModel, int(item_id or 0))
        if not item:
            return

        next_value = original_text
        if original_text.startswith("{"):
            try:
                parsed = json.loads(original_text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                if updated_access_token:
                    parsed["access_token"] = updated_access_token
                if updated_refresh_token:
                    parsed["refresh_token"] = updated_refresh_token
                next_value = json.dumps(parsed, ensure_ascii=False)
        elif updated_access_token:
            next_value = updated_access_token

        if next_value == item.upstream_api_key:
            return
        item.upstream_api_key = next_value
        item.updated_at = _utcnow()
        session.add(item)
        session.commit()


def _build_openai_error_response(message: str, *, status_code: int, code: str) -> JSONResponse:
    error_type = "invalid_request_error" if status_code < 500 else "api_error"
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": str(message or "").strip() or "upstream error",
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _build_openai_chat_completion_response(
    *,
    model: str,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    conversation_id: str = "",
    response_message_id: str = "",
) -> dict[str, Any]:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(model or "").strip(),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": str(content or ""),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
        },
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if response_message_id:
        payload["response_message_id"] = response_message_id
    return payload


def _build_openai_stream_chunk(
    *,
    completion_id: str,
    model: str,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": str(model or "").strip(),
        "choices": [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }
        ],
    }


def _build_openai_models_response(models: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": str(item.get("id") or "").strip(),
                "object": "model",
                "created": 0,
                "owned_by": "openai",
                "title": str(item.get("title") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
            for item in models
            if str(item.get("id") or "").strip()
        ],
    }


def _extract_chunk_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""

    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for field in ("delta", "message"):
            content_holder = choice.get(field)
            if not isinstance(content_holder, dict):
                continue
            content = content_holder.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str) and item:
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = str(item.get("text") or item.get("content") or "").strip()
                        if text:
                            parts.append(text)
    return "".join(parts)


def _resolve_usage_from_json(response_json: dict[str, Any], *, prompt_tokens_estimate: int, completion_fallback: str) -> tuple[int, int]:
    usage = response_json.get("usage")
    if isinstance(usage, dict):
        return (
            _normalize_non_negative_int(usage.get("prompt_tokens", 0), field_name="prompt_tokens"),
            _normalize_non_negative_int(usage.get("completion_tokens", 0), field_name="completion_tokens"),
        )
    return prompt_tokens_estimate, _estimate_text_tokens(completion_fallback)


def _serialize_proxy(proxy: ProxyModel | None) -> Optional[dict]:
    if not proxy:
        return None
    return {
        "id": int(proxy.id or 0),
        "url": proxy.url,
        "region": proxy.region,
        "is_active": proxy.is_active,
    }


def _remaining_tokens(item: SKApiKeyModel) -> Optional[int]:
    if int(item.token_limit or 0) <= 0:
        return None
    return max(0, int(item.token_limit or 0) - int(item.total_tokens_used or 0))


def _serialize_key(item: SKApiKeyModel, *, owner: UserModel | None = None, proxy: ProxyModel | None = None) -> dict:
    resolved_proxy = proxy.url if proxy else str(item.proxy_url or "").strip()
    return {
        "id": int(item.id or 0),
        "user_id": int(item.user_id or 0),
        "owner_username": owner.username if owner else "",
        "name": item.name,
        "description": item.description,
        "key_prefix": item.key_prefix,
        "masked_key": f"{item.key_prefix}...",
        "target_url": item.target_url,
        "has_upstream_api_key": bool(str(item.upstream_api_key or "").strip()),
        "proxy_id": item.proxy_id,
        "proxy_url": str(item.proxy_url or "").strip(),
        "resolved_proxy_url": resolved_proxy,
        "proxy": _serialize_proxy(proxy),
        "token_limit": int(item.token_limit or 0),
        "prompt_tokens_used": int(item.prompt_tokens_used or 0),
        "completion_tokens_used": int(item.completion_tokens_used or 0),
        "total_tokens_used": int(item.total_tokens_used or 0),
        "remaining_tokens": _remaining_tokens(item),
        "request_count": int(item.request_count or 0),
        "is_active": item.is_active,
        "last_used_at": item.last_used_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _resolve_proxy_binding(item: SKApiKeyModel, session: Session) -> tuple[str, ProxyModel | None]:
    if item.proxy_id:
        proxy = session.get(ProxyModel, item.proxy_id)
        if not proxy:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="绑定代理不存在")
        if not proxy.is_active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="绑定代理已停用")
        return proxy.url, proxy

    custom_proxy = normalize_proxy_url(item.proxy_url)
    return str(custom_proxy or "").strip(), None


def _load_key_or_404(key_id: int, session: Session) -> SKApiKeyModel:
    item = session.get(SKApiKeyModel, key_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SK Key 不存在")
    return item


def _ensure_key_access(current_user: UserModel, item: SKApiKeyModel) -> None:
    if current_user.role == "admin":
        return
    if int(current_user.id or 0) != int(item.user_id or 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限访问当前 SK Key")


def _validate_proxy_binding(proxy_id: Optional[int], proxy_url: Optional[str], session: Session) -> tuple[Optional[int], str]:
    normalized_proxy_id = int(proxy_id or 0) or None
    normalized_proxy_url = str(normalize_proxy_url(proxy_url) or "").strip()
    if normalized_proxy_id and normalized_proxy_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="proxy_id 和 proxy_url 只能二选一")
    if normalized_proxy_id:
        proxy = session.get(ProxyModel, normalized_proxy_id)
        if not proxy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="绑定代理不存在")
    return normalized_proxy_id, normalized_proxy_url


def _record_usage(
    session: Session,
    item: SKApiKeyModel,
    *,
    model: str,
    target_url: str,
    proxy_url: str,
    prompt_tokens: int,
    completion_tokens: int,
    success: bool,
    error: str = "",
) -> None:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total_tokens = prompt_tokens + completion_tokens

    item.prompt_tokens_used = int(item.prompt_tokens_used or 0) + prompt_tokens
    item.completion_tokens_used = int(item.completion_tokens_used or 0) + completion_tokens
    item.total_tokens_used = int(item.total_tokens_used or 0) + total_tokens
    item.request_count = int(item.request_count or 0) + 1
    item.last_used_at = _utcnow()
    item.updated_at = _utcnow()
    session.add(item)
    session.add(
        SKApiKeyUsageLog(
            api_key_id=int(item.id or 0),
            user_id=int(item.user_id or 0),
            model=str(model or "").strip(),
            target_url=target_url,
            proxy_url=str(proxy_url or "").strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            success=bool(success),
            error=str(error or "").strip(),
        )
    )
    session.commit()
    session.refresh(item)


def _record_usage_by_key_id(
    api_key_id: int,
    *,
    model: str,
    target_url: str,
    proxy_url: str,
    prompt_tokens: int,
    completion_tokens: int,
    success: bool,
    error: str = "",
) -> None:
    with Session(engine) as session:
        item = session.get(SKApiKeyModel, api_key_id)
        if not item:
            return
        _record_usage(
            session,
            item,
            model=model,
            target_url=target_url,
            proxy_url=proxy_url,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=success,
            error=error,
        )


def _ensure_quota(item: SKApiKeyModel, planned_tokens: int) -> None:
    limit = int(item.token_limit or 0)
    if limit <= 0:
        return
    if int(item.total_tokens_used or 0) + max(0, int(planned_tokens or 0)) > limit:
        remaining = _remaining_tokens(item) or 0
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Token 配额不足，剩余 {remaining}",
        )


@dataclass
class SKAuthContext:
    api_key: SKApiKeyModel
    owner: UserModel
    proxy_url: str
    proxy: ProxyModel | None


@dataclass
class OfficialChatGPTRuntime:
    account: SimpleNamespace
    proxy_url: str
    source: str
    account_id: int = 0
    email: str = ""


def get_sk_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_sk_bearer_scheme),
    session: Session = Depends(get_session),
) -> SKAuthContext:
    if not credentials or str(credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 SK Bearer 令牌")

    raw_key = str(credentials.credentials or "").strip()
    if not raw_key.startswith("sk-"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Bearer 令牌格式无效")

    item = session.exec(select(SKApiKeyModel).where(SKApiKeyModel.key_hash == hash_sk_api_key(raw_key))).first()
    if not item or not item.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Bearer 令牌无效或已停用")

    owner = session.get(UserModel, item.user_id)
    if not owner or not owner.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Key 所属用户不存在或已禁用")

    proxy_url, proxy = _resolve_proxy_binding(item, session)
    return SKAuthContext(api_key=item, owner=owner, proxy_url=proxy_url, proxy=proxy)


def _parse_account_extra_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _resolve_chatgpt_pool_proxy(acc: AccountModel, extra: dict[str, Any], preferred_proxy: str = "") -> str:
    proxy = str(preferred_proxy or "").strip()
    if proxy:
        return proxy

    proxy = str(extra.get("test_proxy") or "").strip()
    if proxy:
        return proxy

    try:
        from core.config_store import config_store

        proxy = str(config_store.get("chatgpt_test_proxy", "") or "").strip()
    except Exception:
        proxy = ""
    if proxy:
        return proxy

    try:
        from core.proxy_pool import proxy_pool

        proxy = proxy_pool.get_next(region=acc.region or "") or proxy_pool.get_next() or ""
    except Exception:
        proxy = ""
    return str(proxy or "").strip()


def _report_chatgpt_runtime_proxy_result(proxy_url: str, *, ok: bool, invalid: bool) -> None:
    normalized_proxy = str(proxy_url or "").strip()
    if not normalized_proxy:
        return

    try:
        from core.proxy_pool import proxy_pool

        if ok or invalid:
            proxy_pool.report_success(normalized_proxy)
        else:
            proxy_pool.report_fail(normalized_proxy)
    except Exception:
        pass


def _persist_chatgpt_pool_account_result(
    account_id: int,
    *,
    updated_access_token: str = "",
    updated_refresh_token: str = "",
    invalid: bool = False,
) -> None:
    with Session(engine) as session:
        acc = session.get(AccountModel, int(account_id or 0))
        if not acc or acc.platform != "chatgpt":
            return

        extra = _parse_account_extra_json(acc.extra_json)
        changed = False
        if updated_access_token:
            extra["access_token"] = str(updated_access_token).strip()
            acc.token = str(updated_access_token).strip()
            changed = True
        if updated_refresh_token:
            extra["refresh_token"] = str(updated_refresh_token).strip()
            changed = True
        if invalid and acc.status != "invalid":
            acc.status = "invalid"
            changed = True

        if changed:
            acc.extra_json = json.dumps(extra, ensure_ascii=False)
        acc.updated_at = _utcnow()
        session.add(acc)
        session.commit()


def _select_chatgpt_pool_runtime(preferred_proxy: str = "") -> OfficialChatGPTRuntime | None:
    global _chatgpt_account_rr_index

    with Session(engine) as session:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.status.in_(AVAILABLE_CHATGPT_ACCOUNT_STATUSES))
            .order_by(AccountModel.id.asc())
        ).all()

    candidates: list[tuple[AccountModel, dict[str, Any]]] = []
    for acc in rows:
        extra = _parse_account_extra_json(acc.extra_json)
        access_token = str(extra.get("access_token") or acc.token or "").strip()
        if not access_token:
            continue
        candidates.append((acc, extra))

    if not candidates:
        return None

    with _chatgpt_account_rr_lock:
        selected_index = _chatgpt_account_rr_index % len(candidates)
        _chatgpt_account_rr_index += 1

    acc, extra = candidates[selected_index]
    return OfficialChatGPTRuntime(
        account=SimpleNamespace(
            email=acc.email,
            access_token=str(extra.get("access_token") or acc.token or "").strip(),
            refresh_token=str(extra.get("refresh_token") or "").strip(),
            id_token=str(extra.get("id_token") or "").strip(),
            session_token=str(extra.get("session_token") or "").strip(),
            client_id=str(extra.get("client_id") or DEFAULT_CHATGPT_CLIENT_ID).strip() or DEFAULT_CHATGPT_CLIENT_ID,
            cookies=str(extra.get("cookies") or "").strip(),
        ),
        proxy_url=_resolve_chatgpt_pool_proxy(acc, extra, preferred_proxy=preferred_proxy),
        source="account_pool",
        account_id=int(acc.id or 0),
        email=acc.email,
    )


def _resolve_official_chatgpt_runtime(auth: SKAuthContext) -> OfficialChatGPTRuntime:
    upstream_value = str(auth.api_key.upstream_api_key or "").strip()
    if upstream_value:
        return OfficialChatGPTRuntime(
            account=_build_chatgpt_account_from_upstream_auth(upstream_value),
            proxy_url=auth.proxy_url,
            source="sk_upstream",
        )

    runtime = _select_chatgpt_pool_runtime(preferred_proxy=auth.proxy_url)
    if runtime:
        return runtime

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="官方 ChatGPT 模式既未配置 Upstream API Key，也没有可用的 ChatGPT 账号池",
    )


def _persist_official_chatgpt_runtime(
    auth: SKAuthContext,
    runtime: OfficialChatGPTRuntime,
    *,
    updated_access_token: str = "",
    updated_refresh_token: str = "",
    invalid: bool = False,
) -> None:
    if runtime.source == "account_pool":
        _persist_chatgpt_pool_account_result(
            runtime.account_id,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
            invalid=invalid,
        )
        return

    _persist_chatgpt_upstream_tokens(
        int(auth.api_key.id or 0),
        auth.api_key.upstream_api_key,
        updated_access_token=updated_access_token,
        updated_refresh_token=updated_refresh_token,
    )


class SKApiKeyCreateRequest(BaseModel):
    name: str
    description: str = ""
    owner_user_id: Optional[int] = None
    target_url: str = ""
    upstream_api_key: str = ""
    proxy_id: Optional[int] = None
    proxy_url: Optional[str] = None
    token_limit: int = 0
    is_active: bool = True


class SKApiKeyUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner_user_id: Optional[int] = None
    target_url: Optional[str] = None
    upstream_api_key: Optional[str] = None
    proxy_id: Optional[int] = None
    proxy_url: Optional[str] = None
    token_limit: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("/sk-keys")
def list_sk_keys(
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(SKApiKeyModel).order_by(SKApiKeyModel.id.desc())
    if current_user.role != "admin":
        query = query.where(SKApiKeyModel.user_id == int(current_user.id or 0))

    items = session.exec(query).all()
    owner_ids = {int(item.user_id or 0) for item in items}
    proxy_ids = {int(item.proxy_id or 0) for item in items if item.proxy_id}
    owners = {
        int(user.id or 0): user
        for user in session.exec(select(UserModel).where(UserModel.id.in_(owner_ids))).all()
    } if owner_ids else {}
    proxies = {
        int(proxy.id or 0): proxy
        for proxy in session.exec(select(ProxyModel).where(ProxyModel.id.in_(proxy_ids))).all()
    } if proxy_ids else {}

    return {
        "items": [
            _serialize_key(item, owner=owners.get(int(item.user_id or 0)), proxy=proxies.get(int(item.proxy_id or 0)))
            for item in items
        ]
    }


@router.post("/sk-keys")
def create_sk_key(
    body: SKApiKeyCreateRequest,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SK Key 名称不能为空")

    owner_user_id = int(body.owner_user_id or current_user.id or 0)
    if current_user.role != "admin" and owner_user_id != int(current_user.id or 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="普通用户只能为自己创建 SK Key")

    owner = session.get(UserModel, owner_user_id)
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="所属用户不存在")

    proxy_id, proxy_url = _validate_proxy_binding(body.proxy_id, body.proxy_url, session)
    raw_key = generate_sk_api_key()
    item = SKApiKeyModel(
        user_id=owner_user_id,
        name=name,
        description=str(body.description or "").strip(),
        key_prefix=raw_key[:12],
        key_hash=hash_sk_api_key(raw_key),
        target_url=_normalize_chat_url(body.target_url),
        upstream_api_key=str(body.upstream_api_key or "").strip(),
        proxy_id=proxy_id,
        proxy_url=proxy_url,
        token_limit=_normalize_non_negative_int(body.token_limit, field_name="token_limit"),
        is_active=bool(body.is_active),
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    proxy = session.get(ProxyModel, proxy_id) if proxy_id else None
    return {
        "item": _serialize_key(item, owner=owner, proxy=proxy),
        "secret_key": raw_key,
    }


@router.get("/sk-keys/{key_id}")
def get_sk_key(
    key_id: int,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = _load_key_or_404(key_id, session)
    _ensure_key_access(current_user, item)
    owner = session.get(UserModel, item.user_id)
    proxy = session.get(ProxyModel, item.proxy_id) if item.proxy_id else None
    return {"item": _serialize_key(item, owner=owner, proxy=proxy)}


@router.patch("/sk-keys/{key_id}")
def update_sk_key(
    key_id: int,
    body: SKApiKeyUpdateRequest,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = _load_key_or_404(key_id, session)
    _ensure_key_access(current_user, item)

    if body.owner_user_id is not None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="普通用户不能转移 SK Key 所属用户")
        owner = session.get(UserModel, int(body.owner_user_id))
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="所属用户不存在")
        item.user_id = int(owner.id or 0)

    if body.name is not None:
        name = str(body.name or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SK Key 名称不能为空")
        item.name = name
    if body.description is not None:
        item.description = str(body.description or "").strip()
    if body.target_url is not None:
        item.target_url = _normalize_chat_url(body.target_url)
    if body.upstream_api_key is not None:
        item.upstream_api_key = str(body.upstream_api_key or "").strip()
    if body.token_limit is not None:
        item.token_limit = _normalize_non_negative_int(body.token_limit, field_name="token_limit")
    if body.is_active is not None:
        item.is_active = bool(body.is_active)

    if body.proxy_id is not None or body.proxy_url is not None:
        proxy_id, proxy_url = _validate_proxy_binding(body.proxy_id, body.proxy_url, session)
        item.proxy_id = proxy_id
        item.proxy_url = proxy_url

    item.updated_at = _utcnow()
    session.add(item)
    session.commit()
    session.refresh(item)

    owner = session.get(UserModel, item.user_id)
    proxy = session.get(ProxyModel, item.proxy_id) if item.proxy_id else None
    return {"item": _serialize_key(item, owner=owner, proxy=proxy)}


@router.post("/sk-keys/{key_id}/rotate")
def rotate_sk_key(
    key_id: int,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = _load_key_or_404(key_id, session)
    _ensure_key_access(current_user, item)

    raw_key = generate_sk_api_key()
    item.key_prefix = raw_key[:12]
    item.key_hash = hash_sk_api_key(raw_key)
    item.updated_at = _utcnow()
    session.add(item)
    session.commit()
    session.refresh(item)

    owner = session.get(UserModel, item.user_id)
    proxy = session.get(ProxyModel, item.proxy_id) if item.proxy_id else None
    return {
        "item": _serialize_key(item, owner=owner, proxy=proxy),
        "secret_key": raw_key,
    }


@router.delete("/sk-keys/{key_id}")
def delete_sk_key(
    key_id: int,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = _load_key_or_404(key_id, session)
    _ensure_key_access(current_user, item)
    session.delete(item)
    session.commit()
    return {"ok": True}


@router.get("/sk-keys/{key_id}/usage")
def get_sk_key_usage(
    key_id: int,
    limit: int = 20,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = _load_key_or_404(key_id, session)
    _ensure_key_access(current_user, item)
    size = max(1, min(int(limit or 20), 100))
    owner = session.get(UserModel, item.user_id)
    proxy = session.get(ProxyModel, item.proxy_id) if item.proxy_id else None
    rows = session.exec(
        select(SKApiKeyUsageLog)
        .where(SKApiKeyUsageLog.api_key_id == key_id)
        .order_by(SKApiKeyUsageLog.id.desc())
        .limit(size)
    ).all()
    return {
        "summary": _serialize_key(item, owner=owner, proxy=proxy),
        "items": [
            {
                "id": int(row.id or 0),
                "model": row.model,
                "target_url": row.target_url,
                "proxy_url": row.proxy_url,
                "prompt_tokens": row.prompt_tokens,
                "completion_tokens": row.completion_tokens,
                "total_tokens": row.total_tokens,
                "success": row.success,
                "error": row.error,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.post("/sk/authorize")
def authorize_sk_key(auth: SKAuthContext = Depends(get_sk_auth_context)):
    return {
        "ok": True,
        "user": {
            "id": int(auth.owner.id or 0),
            "username": auth.owner.username,
            "role": auth.owner.role,
        },
        "api_key": _serialize_key(auth.api_key, owner=auth.owner, proxy=auth.proxy),
    }


def _build_upstream_headers(auth: SKAuthContext) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    upstream_api_key = str(auth.api_key.upstream_api_key or "").strip()
    if upstream_api_key:
        headers["Authorization"] = f"Bearer {upstream_api_key}"
    return headers


def _build_upstream_payload(payload: dict[str, Any], model: str, messages: list[dict[str, Any]], *, stream: bool) -> dict[str, Any]:
    request_payload = dict(payload)
    request_payload.pop("target_url", None)
    request_payload["model"] = model
    request_payload["messages"] = messages
    request_payload["stream"] = stream
    return request_payload


def _handle_official_chatgpt_models(auth: SKAuthContext, target_url: str) -> Response:
    from platforms.chatgpt.message_tester import fetch_available_models

    runtime = _resolve_official_chatgpt_runtime(auth)
    result = fetch_available_models(runtime.account, proxy=runtime.proxy_url, target_url=target_url)
    _persist_official_chatgpt_runtime(
        auth,
        runtime,
        updated_access_token=result.updated_access_token,
        updated_refresh_token=result.updated_refresh_token,
        invalid=bool(result.invalid),
    )

    models_url = str(result.models_url or _derive_models_url(target_url)).strip() or OFFICIAL_CHATGPT_MODELS_URL
    success = bool(result.ok)
    error_text = "" if success else str(result.message or "")[:500]
    _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=success, invalid=bool(result.invalid))
    _record_usage_by_key_id(
        int(auth.api_key.id or 0),
        model="models",
        target_url=models_url,
        proxy_url=runtime.proxy_url,
        prompt_tokens=0,
        completion_tokens=0,
        success=success,
        error=error_text,
    )

    if not success:
        return _build_openai_error_response(
            str(result.message or "获取 ChatGPT 模型列表失败"),
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="chatgpt_official_models_error",
        )

    return JSONResponse(content=_build_openai_models_response(result.models))


def _handle_official_chatgpt_completion(
    *,
    auth: SKAuthContext,
    payload: dict[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    target_url: str,
    prompt_tokens_estimate: int,
) -> Response:
    from platforms.chatgpt.message_tester import send_chat_message

    prompt = _openai_messages_to_prompt(messages)
    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="messages 内没有可转发的文本内容")

    runtime = _resolve_official_chatgpt_runtime(auth)
    result = send_chat_message(
        runtime.account,
        proxy=runtime.proxy_url,
        prompt=prompt,
        model=model,
        target_url=target_url,
        history_and_training_disabled=bool(payload.get("history_and_training_disabled")),
        archive_after_send=bool(payload.get("archive_after_send")),
    )
    _persist_official_chatgpt_runtime(
        auth,
        runtime,
        updated_access_token=result.updated_access_token,
        updated_refresh_token=result.updated_refresh_token,
        invalid=bool(result.invalid),
    )

    success = bool(result.ok)
    completion_tokens = _estimate_text_tokens(result.response_text) if success else 0
    _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=success, invalid=bool(result.invalid))
    _record_usage_by_key_id(
        int(auth.api_key.id or 0),
        model=model,
        target_url=target_url,
        proxy_url=runtime.proxy_url,
        prompt_tokens=prompt_tokens_estimate if success else 0,
        completion_tokens=completion_tokens,
        success=success,
        error="" if success else str(result.message or "")[:500],
    )

    if not success:
        return _build_openai_error_response(
            str(result.message or "ChatGPT 官方会话失败"),
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="chatgpt_official_completion_error",
        )

    return JSONResponse(
        content=_build_openai_chat_completion_response(
            model=model,
            content=result.response_text,
            prompt_tokens=prompt_tokens_estimate,
            completion_tokens=completion_tokens,
            conversation_id=result.conversation_id,
            response_message_id=result.response_message_id,
        )
    )


def _handle_official_chatgpt_stream_completion(
    *,
    auth: SKAuthContext,
    payload: dict[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    target_url: str,
    prompt_tokens_estimate: int,
) -> Response:
    from platforms.chatgpt.message_tester import stream_chat_message

    prompt = _openai_messages_to_prompt(messages)
    if not prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="messages 内没有可转发的文本内容")

    runtime = _resolve_official_chatgpt_runtime(auth)
    upstream_events = stream_chat_message(
        runtime.account,
        proxy=runtime.proxy_url,
        prompt=prompt,
        model=model,
        target_url=target_url,
        history_and_training_disabled=bool(payload.get("history_and_training_disabled")),
    )
    first_event = next(upstream_events, None)
    if not first_event:
        _record_usage_by_key_id(
            int(auth.api_key.id or 0),
            model=model,
            target_url=target_url,
            proxy_url=runtime.proxy_url,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error="ChatGPT 官方会话未返回任何事件",
        )
        _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=False, invalid=False)
        return _build_openai_error_response(
            "ChatGPT 官方会话未返回任何事件",
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="chatgpt_official_stream_empty",
        )

    if str(first_event.get("event") or "") == "error":
        data = first_event.get("data") if isinstance(first_event, dict) else {}
        data = data if isinstance(data, dict) else {}
        invalid = bool(data.get("invalid"))
        _persist_official_chatgpt_runtime(
            auth,
            runtime,
            updated_access_token=str(data.get("updated_access_token") or ""),
            updated_refresh_token=str(data.get("updated_refresh_token") or ""),
            invalid=invalid,
        )
        error_message = str(data.get("message") or "ChatGPT 官方流式会话失败")
        _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=False, invalid=invalid)
        _record_usage_by_key_id(
            int(auth.api_key.id or 0),
            model=model,
            target_url=target_url,
            proxy_url=runtime.proxy_url,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=error_message[:500],
        )
        return _build_openai_error_response(
            error_message,
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="chatgpt_official_stream_error",
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    def iter_events():
        yield first_event
        for event in upstream_events:
            yield event

    def event_stream():
        accumulated_text = ""
        recorded = False
        sent_role = False

        try:
            for event in iter_events():
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("event") or "").strip()
                data = event.get("data")
                data = data if isinstance(data, dict) else {}

                if event_type == "meta":
                    continue

                if event_type == "delta":
                    delta_text = str(data.get("delta") or "")
                    if not delta_text:
                        continue
                    accumulated_text += delta_text
                    chunk_delta: dict[str, Any] = {"content": delta_text}
                    if not sent_role:
                        chunk_delta["role"] = "assistant"
                        sent_role = True
                    yield f"data: {json.dumps(_build_openai_stream_chunk(completion_id=completion_id, model=model, delta=chunk_delta), ensure_ascii=False)}\n\n"
                    continue

                if event_type == "done":
                    response_text = str(data.get("response_text") or accumulated_text)
                    if response_text and not accumulated_text:
                        accumulated_text = response_text
                        yield f"data: {json.dumps(_build_openai_stream_chunk(completion_id=completion_id, model=model, delta={'role': 'assistant', 'content': response_text}), ensure_ascii=False)}\n\n"
                        sent_role = True

                    invalid = bool(data.get("invalid"))
                    _persist_official_chatgpt_runtime(
                        auth,
                        runtime,
                        updated_access_token=str(data.get("updated_access_token") or ""),
                        updated_refresh_token=str(data.get("updated_refresh_token") or ""),
                        invalid=invalid,
                    )
                    _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=True, invalid=invalid)
                    _record_usage_by_key_id(
                        int(auth.api_key.id or 0),
                        model=model,
                        target_url=target_url,
                        proxy_url=runtime.proxy_url,
                        prompt_tokens=prompt_tokens_estimate,
                        completion_tokens=_estimate_text_tokens(response_text),
                        success=True,
                        error="",
                    )
                    recorded = True
                    yield f"data: {json.dumps(_build_openai_stream_chunk(completion_id=completion_id, model=model, finish_reason='stop'), ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                if event_type == "error":
                    error_message = str(data.get("message") or "ChatGPT 官方流式会话失败")
                    invalid = bool(data.get("invalid"))
                    _persist_official_chatgpt_runtime(
                        auth,
                        runtime,
                        updated_access_token=str(data.get("updated_access_token") or ""),
                        updated_refresh_token=str(data.get("updated_refresh_token") or ""),
                        invalid=invalid,
                    )
                    _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=False, invalid=invalid)
                    _record_usage_by_key_id(
                        int(auth.api_key.id or 0),
                        model=model,
                        target_url=target_url,
                        proxy_url=runtime.proxy_url,
                        prompt_tokens=0,
                        completion_tokens=0,
                        success=False,
                        error=error_message[:500],
                    )
                    recorded = True
                    yield f"data: {json.dumps({'error': {'message': error_message, 'type': 'api_error', 'param': None, 'code': 'chatgpt_official_stream_error'}}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
        finally:
            if not recorded:
                _record_usage_by_key_id(
                    int(auth.api_key.id or 0),
                    model=model,
                    target_url=target_url,
                    proxy_url=runtime.proxy_url,
                    prompt_tokens=0,
                    completion_tokens=0,
                    success=False,
                    error="ChatGPT 官方流式会话在完成前中断",
                )
                _report_chatgpt_runtime_proxy_result(runtime.proxy_url, ok=False, invalid=False)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sk/models")
@openai_router.get("/models")
def list_openai_models(auth: SKAuthContext = Depends(get_sk_auth_context)):
    target_url = _normalize_chat_url(auth.api_key.target_url)
    if _is_official_chatgpt_target(target_url):
        return _handle_official_chatgpt_models(auth, target_url)

    import requests

    models_url = _derive_models_url(target_url)
    response = requests.get(
        models_url,
        headers=_build_upstream_headers(auth),
        proxies=build_requests_proxy_config(auth.proxy_url),
        timeout=60,
    )
    success = response.status_code < 400
    error_text = "" if success else str(response.text or "")[:500]
    _record_usage_by_key_id(
        int(auth.api_key.id or 0),
        model="models",
        target_url=models_url,
        proxy_url=auth.proxy_url,
        prompt_tokens=0,
        completion_tokens=0,
        success=success,
        error=error_text,
    )

    response_content_type = str(response.headers.get("Content-Type", "") or "")
    if "json" in response_content_type.lower():
        try:
            return JSONResponse(content=response.json(), status_code=response.status_code)
        except ValueError:
            pass
    return Response(
        content=str(response.text or ""),
        status_code=response.status_code,
        media_type=response_content_type or "text/plain",
    )


@router.post("/sk/chat/completions")
@openai_router.post("/chat/completions")
async def sk_chat_completions(
    request: Request,
    auth: SKAuthContext = Depends(get_sk_auth_context),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON 对象")

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="messages 不能为空")

    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model 不能为空")

    target_url = _normalize_chat_url(payload.get("target_url") or auth.api_key.target_url or "")

    stream = bool(payload.get("stream"))
    prompt_tokens_estimate = _estimate_chat_tokens(messages)
    planned_completion_tokens = _planned_completion_tokens(payload)
    _ensure_quota(auth.api_key, prompt_tokens_estimate + planned_completion_tokens)

    if _is_official_chatgpt_target(target_url):
        if stream:
            return _handle_official_chatgpt_stream_completion(
                auth=auth,
                payload=payload,
                model=model,
                messages=messages,
                target_url=target_url,
                prompt_tokens_estimate=prompt_tokens_estimate,
            )
        return _handle_official_chatgpt_completion(
            auth=auth,
            payload=payload,
            model=model,
            messages=messages,
            target_url=target_url,
            prompt_tokens_estimate=prompt_tokens_estimate,
        )

    import requests

    request_payload = _build_upstream_payload(payload, model, messages, stream=stream)
    request_headers = _build_upstream_headers(auth)
    request_kwargs = {
        "json": request_payload,
        "headers": request_headers,
        "proxies": build_requests_proxy_config(auth.proxy_url),
        "timeout": 180,
    }

    if not stream:
        response = requests.post(target_url, **request_kwargs)
        response_content_type = str(response.headers.get("Content-Type", "") or "")
        response_json: dict[str, Any] | None = None
        response_text = str(response.text or "")
        if "json" in response_content_type.lower():
            try:
                response_json = response.json()
            except ValueError:
                response_json = None

        success = response.status_code < 400
        if response_json is not None and success:
            prompt_tokens, completion_tokens = _resolve_usage_from_json(
                response_json,
                prompt_tokens_estimate=prompt_tokens_estimate,
                completion_fallback=response_text,
            )
        elif success:
            prompt_tokens, completion_tokens = prompt_tokens_estimate, _estimate_text_tokens(response_text)
        else:
            prompt_tokens, completion_tokens = 0, 0

        _record_usage_by_key_id(
            int(auth.api_key.id or 0),
            model=model,
            target_url=target_url,
            proxy_url=auth.proxy_url,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=success,
            error="" if success else response_text[:500],
        )

        if response_json is not None:
            return JSONResponse(content=response_json, status_code=response.status_code)

        return Response(
            content=response_text,
            status_code=response.status_code,
            media_type=response_content_type or "text/plain",
        )

    upstream_response = requests.post(target_url, stream=True, **request_kwargs)
    if upstream_response.status_code >= 400:
        response_content_type = str(upstream_response.headers.get("Content-Type", "") or "")
        response_text = str(upstream_response.text or "")
        _record_usage_by_key_id(
            int(auth.api_key.id or 0),
            model=model,
            target_url=target_url,
            proxy_url=auth.proxy_url,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=response_text[:500],
        )
        if "json" in response_content_type.lower():
            try:
                return JSONResponse(content=upstream_response.json(), status_code=upstream_response.status_code)
            except ValueError:
                pass
        return Response(
            content=response_text,
            status_code=upstream_response.status_code,
            media_type=response_content_type or "text/plain",
        )

    def event_stream():
        accumulated_text = ""
        prompt_tokens = prompt_tokens_estimate
        completion_tokens = 0
        error_text = ""
        try:
            for raw_line in upstream_response.iter_lines(decode_unicode=True):
                line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="ignore")
                if line.startswith("data:"):
                    payload_text = line[5:].strip()
                    if payload_text and payload_text != "[DONE]":
                        try:
                            chunk = json.loads(payload_text)
                        except ValueError:
                            chunk = None
                        if isinstance(chunk, dict):
                            usage = chunk.get("usage")
                            if isinstance(usage, dict):
                                prompt_tokens = _normalize_non_negative_int(usage.get("prompt_tokens", prompt_tokens), field_name="prompt_tokens")
                                completion_tokens = _normalize_non_negative_int(usage.get("completion_tokens", completion_tokens), field_name="completion_tokens")
                            else:
                                accumulated_text += _extract_chunk_text(chunk)
                yield f"{line}\n"

            if completion_tokens <= 0 and accumulated_text:
                completion_tokens = _estimate_text_tokens(accumulated_text)
        except Exception as exc:
            error_text = str(exc)[:500]
            raise
        finally:
            upstream_response.close()
            _record_usage_by_key_id(
                int(auth.api_key.id or 0),
                model=model,
                target_url=target_url,
                proxy_url=auth.proxy_url,
                prompt_tokens=0 if error_text else prompt_tokens,
                completion_tokens=0 if error_text else completion_tokens,
                success=not bool(error_text),
                error=error_text,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
