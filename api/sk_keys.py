import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlmodel import Session, func, select

from core.db import ApiAccessLog, AccountModel, ProxyModel, SKApiKeyModel, SKApiKeyUsageLog, UserModel, engine, get_session
from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url
from core.security import (
    decode_access_token,
    generate_sk_api_key,
    get_current_user,
    hash_sk_api_key,
)

router = APIRouter(tags=["sk"])
openai_router = APIRouter(prefix="/v1", tags=["openai"])
anthropic_apps_router = APIRouter(prefix="/apps/anthropic", tags=["anthropic-apps"])
_sk_bearer_scheme = HTTPBearer(auto_error=False)
OFFICIAL_CHATGPT_BASE = "https://chatgpt.com"
OFFICIAL_CHATGPT_CONVERSATION_URL = f"{OFFICIAL_CHATGPT_BASE}/backend-api/conversation"
OFFICIAL_CHATGPT_MODELS_URL = f"{OFFICIAL_CHATGPT_BASE}/backend-api/models"
DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AVAILABLE_CHATGPT_ACCOUNT_STATUSES = ("registered", "trial", "subscribed")
_chatgpt_account_rr_lock = threading.Lock()
_chatgpt_account_rr_index = 0
_responses_store_lock = threading.Lock()
_responses_store: dict[str, dict[str, Any]] = {}
USD_PER_1M_TOKENS = Decimal("2")
TOKENS_PER_USD = Decimal("500000")
USD_PRECISION = Decimal("0.000001")


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


def _normalize_non_negative_decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} 必须是数字")
    if normalized < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} 不能小于 0")
    return normalized


def _usd_to_token_limit(value: Any, *, field_name: str = "usd_limit") -> int:
    usd_value = _normalize_non_negative_decimal(value, field_name=field_name)
    if usd_value <= 0:
        return 0
    return int((usd_value * TOKENS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _tokens_to_usd(value: Any) -> float:
    tokens = max(0, int(value or 0))
    usd_value = (Decimal(tokens) * USD_PER_1M_TOKENS / Decimal("1000000")).quantize(
        USD_PRECISION,
        rounding=ROUND_HALF_UP,
    )
    return float(usd_value)


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
        path = _normalize_official_chatgpt_path(path, endpoint="conversation")
        return urlunsplit(parts._replace(path=path))

    if path.endswith("/chat/completions"):
        return urlunsplit(parts._replace(path=path))

    next_path = f"{path}/chat/completions" if path else "/chat/completions"
    return urlunsplit(parts._replace(path=next_path))


def _normalize_official_chatgpt_path(path: str, *, endpoint: str) -> str:
    normalized = str(path or "").rstrip("/")
    if not normalized or normalized == "/":
        return f"/backend-api/{endpoint}"
    if normalized.endswith("/backend-api"):
        return f"{normalized}/{endpoint}"

    if endpoint == "conversation":
        if normalized.endswith("/backend-api/conversation"):
            return normalized
        if normalized.endswith("/backend-api/conversation/models"):
            return normalized[:-len("/models")]
        if normalized.endswith("/backend-api/models"):
            prefix = normalized[:-len("/backend-api/models")]
            return f"{prefix}/backend-api/conversation" if prefix else "/backend-api/conversation"
        return normalized

    if normalized.endswith("/backend-api/models"):
        return normalized
    if normalized.endswith("/backend-api/conversation/models"):
        prefix = normalized[:-len("/backend-api/conversation/models")]
        return f"{prefix}/backend-api/models" if prefix else "/backend-api/models"
    if normalized.endswith("/backend-api/conversation"):
        prefix = normalized[:-len("/backend-api/conversation")]
        return f"{prefix}/backend-api/models" if prefix else "/backend-api/models"
    return normalized


def _derive_models_url(target_url: str) -> str:
    normalized = _normalize_chat_url(target_url)
    parts = urlsplit(normalized)
    path = str(parts.path or "")
    host = str(parts.netloc or "").lower()
    if host.endswith("chatgpt.com"):
        path = _normalize_official_chatgpt_path(path, endpoint="models")
    elif path.endswith("/backend-api/conversation"):
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


def _build_anthropic_error_response(message: str, *, status_code: int, error_type: str | None = None) -> JSONResponse:
    if not error_type:
        if status_code == 401:
            error_type = "authentication_error"
        elif status_code == 403:
            error_type = "permission_error"
        elif status_code == 429:
            error_type = "rate_limit_error"
        elif status_code >= 500:
            error_type = "api_error"
        else:
            error_type = "invalid_request_error"
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": str(message or "").strip() or "request failed",
            },
        },
    )


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


def _resolve_sk_auth_context_from_raw_key(raw_key: str, session: Session) -> "SKAuthContext":
    normalized_key = str(raw_key or "").strip()
    if not normalized_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 SK Bearer 令牌")
    if not normalized_key.startswith("sk-"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Bearer 令牌格式无效")

    item = session.exec(select(SKApiKeyModel).where(SKApiKeyModel.key_hash == hash_sk_api_key(normalized_key))).first()
    if not item or not item.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Bearer 令牌无效或已停用")

    owner = session.get(UserModel, item.user_id)
    if not owner or not owner.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SK Key 所属用户不存在或已禁用")

    proxy_url, proxy = _resolve_proxy_binding(item, session)
    return SKAuthContext(api_key=item, owner=owner, proxy_url=proxy_url, proxy=proxy)


def _remaining_tokens(item: SKApiKeyModel) -> Optional[int]:
    if int(item.token_limit or 0) <= 0:
        return None
    return max(0, int(item.token_limit or 0) - int(item.total_tokens_used or 0))


def _serialize_key(item: SKApiKeyModel, *, owner: UserModel | None = None, proxy: ProxyModel | None = None) -> dict:
    resolved_proxy = proxy.url if proxy else str(item.proxy_url or "").strip()
    total_tokens_used = int(item.total_tokens_used or 0)
    token_limit = int(item.token_limit or 0)
    remaining_tokens = _remaining_tokens(item)
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
        "token_limit": token_limit,
        "usd_limit": _tokens_to_usd(token_limit) if token_limit > 0 else None,
        "prompt_tokens_used": int(item.prompt_tokens_used or 0),
        "completion_tokens_used": int(item.completion_tokens_used or 0),
        "total_tokens_used": total_tokens_used,
        "usd_used": _tokens_to_usd(total_tokens_used),
        "remaining_tokens": remaining_tokens,
        "usd_remaining": _tokens_to_usd(remaining_tokens) if remaining_tokens is not None else None,
        "usd_rate_per_1m_tokens": float(USD_PER_1M_TOKENS),
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


def _should_record_api_access(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    return (
        normalized.startswith("/v1/")
        or normalized.startswith("/apps/anthropic/")
        or normalized.startswith("/api/sk/")
    )


def _extract_request_client_ip(request: Request) -> str:
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = str(request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    return str(host or "").strip()


def _serialize_access_log(item: ApiAccessLog) -> dict[str, Any]:
    return {
        "id": int(item.id or 0),
        "actor_type": item.actor_type,
        "user_id": item.user_id,
        "username": item.username,
        "api_key_id": item.api_key_id,
        "api_key_name": item.api_key_name,
        "api_key_prefix": item.api_key_prefix,
        "method": item.method,
        "path": item.path,
        "status_code": int(item.status_code or 0),
        "success": bool(item.success),
        "client_ip": item.client_ip,
        "user_agent": item.user_agent,
        "target_url": item.target_url,
        "model": item.model,
        "error": item.error,
        "duration_ms": int(item.duration_ms or 0),
        "created_at": item.created_at,
    }


def _record_api_access(
    *,
    request: Request,
    status_code: int,
    auth: Optional["SKAuthContext"] = None,
    current_user: UserModel | None = None,
    model: str = "",
    target_url: str = "",
    error: str = "",
    duration_ms: int = 0,
) -> None:
    path = str(request.url.path or "").strip()
    if not _should_record_api_access(path):
        return

    actor_type = "anonymous"
    user_id: int | None = None
    username = ""
    api_key_id: int | None = None
    api_key_name = ""
    api_key_prefix = ""

    if auth:
        actor_type = "sk_key"
        user_id = int(auth.owner.id or 0) or None
        username = str(auth.owner.username or "").strip()
        api_key_id = int(auth.api_key.id or 0) or None
        api_key_name = str(auth.api_key.name or "").strip()
        api_key_prefix = str(auth.api_key.key_prefix or "").strip()
        if not target_url:
            target_url = str(auth.api_key.target_url or "").strip()
    elif current_user:
        actor_type = "user"
        user_id = int(current_user.id or 0) or None
        username = str(current_user.username or "").strip()
    else:
        auth_header = str(request.headers.get("authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            raw_token = auth_header[7:].strip()
            if raw_token.startswith("sk-"):
                with Session(engine) as session:
                    try:
                        auth_context = _resolve_sk_auth_context_from_raw_key(raw_token, session)
                    except HTTPException:
                        auth_context = None
                if auth_context:
                    actor_type = "sk_key"
                    user_id = int(auth_context.owner.id or 0) or None
                    username = str(auth_context.owner.username or "").strip()
                    api_key_id = int(auth_context.api_key.id or 0) or None
                    api_key_name = str(auth_context.api_key.name or "").strip()
                    api_key_prefix = str(auth_context.api_key.key_prefix or "").strip()
                    if not target_url:
                        target_url = str(auth_context.api_key.target_url or "").strip()
            else:
                try:
                    payload = decode_access_token(raw_token)
                except HTTPException:
                    payload = {}
                resolved_user_id = int(payload.get("uid") or 0)
                if resolved_user_id > 0:
                    with Session(engine) as session:
                        user = session.get(UserModel, resolved_user_id)
                    if user and user.is_active:
                        actor_type = "user"
                        user_id = int(user.id or 0) or None
                        username = str(user.username or "").strip()
        else:
            raw_key = str(request.headers.get("x-api-key") or request.headers.get("anthropic-api-key") or "").strip()
            if raw_key:
                with Session(engine) as session:
                    try:
                        auth_context = _resolve_sk_auth_context_from_raw_key(raw_key, session)
                    except HTTPException:
                        auth_context = None
                if auth_context:
                    actor_type = "sk_key"
                    user_id = int(auth_context.owner.id or 0) or None
                    username = str(auth_context.owner.username or "").strip()
                    api_key_id = int(auth_context.api_key.id or 0) or None
                    api_key_name = str(auth_context.api_key.name or "").strip()
                    api_key_prefix = str(auth_context.api_key.key_prefix or "").strip()
                    if not target_url:
                        target_url = str(auth_context.api_key.target_url or "").strip()

    with Session(engine) as session:
        session.add(
            ApiAccessLog(
                actor_type=actor_type,
                user_id=user_id,
                username=username,
                api_key_id=api_key_id,
                api_key_name=api_key_name,
                api_key_prefix=api_key_prefix,
                method=str(request.method or "").upper(),
                path=path,
                status_code=int(status_code or 0),
                success=int(status_code or 0) < 400,
                client_ip=_extract_request_client_ip(request),
                user_agent=str(request.headers.get("user-agent") or "").strip(),
                target_url=str(target_url or "").strip(),
                model=str(model or "").strip(),
                error=str(error or "").strip()[:500],
                duration_ms=max(0, int(duration_ms or 0)),
            )
        )
        session.commit()


def _ensure_quota(item: SKApiKeyModel, planned_tokens: int) -> None:
    limit = int(item.token_limit or 0)
    if limit <= 0:
        return
    if int(item.total_tokens_used or 0) + max(0, int(planned_tokens or 0)) > limit:
        remaining = _remaining_tokens(item) or 0
        remaining_usd = _tokens_to_usd(remaining)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"额度不足，剩余 ${remaining_usd:.6f}（约 {remaining} tokens）",
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
    return _resolve_sk_auth_context_from_raw_key(str(credentials.credentials or "").strip(), session)


def get_sk_auth_context_from_request(
    request: Request,
    session: Session = Depends(get_session),
) -> SKAuthContext:
    auth_header = str(request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        raw_key = auth_header[7:].strip()
    else:
        raw_key = str(
            request.headers.get("x-api-key")
            or request.headers.get("anthropic-api-key")
            or ""
        ).strip()

    return _resolve_sk_auth_context_from_raw_key(raw_key, session)


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
    usd_limit: Optional[float] = None
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
    usd_limit: Optional[float] = None
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
        token_limit=(
            _usd_to_token_limit(body.usd_limit)
            if body.usd_limit is not None
            else _normalize_non_negative_int(body.token_limit, field_name="token_limit")
        ),
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
    if body.usd_limit is not None:
        item.token_limit = _usd_to_token_limit(body.usd_limit)
    elif body.token_limit is not None:
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


@router.get("/access-logs")
def list_access_logs(
    page: int = 1,
    page_size: int = 20,
    api_key_id: Optional[int] = None,
    user_id: Optional[int] = None,
    path: str = "",
    success: Optional[bool] = None,
    current_user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    current_page = max(1, int(page or 1))
    size = max(1, min(int(page_size or 20), 100))
    conditions = []

    if current_user.role != "admin":
        conditions.append(ApiAccessLog.user_id == int(current_user.id or 0))
    elif user_id:
        conditions.append(ApiAccessLog.user_id == int(user_id))

    if api_key_id:
        conditions.append(ApiAccessLog.api_key_id == int(api_key_id))
    if str(path or "").strip():
        conditions.append(ApiAccessLog.path.contains(str(path).strip()))
    if success is not None:
        conditions.append(ApiAccessLog.success == bool(success))

    list_statement = select(ApiAccessLog)
    count_statement = select(func.count()).select_from(ApiAccessLog)
    for condition in conditions:
        list_statement = list_statement.where(condition)
        count_statement = count_statement.where(condition)

    total = int(session.exec(count_statement).one() or 0)
    rows = session.exec(
        list_statement
        .order_by(ApiAccessLog.id.desc())
        .offset((current_page - 1) * size)
        .limit(size)
    ).all()

    return {
        "page": current_page,
        "page_size": size,
        "total": total,
        "items": [_serialize_access_log(item) for item in rows],
    }


@router.post("/sk/authorize")
def authorize_sk_key(
    request: Request,
    auth: SKAuthContext = Depends(get_sk_auth_context),
):
    response_payload = {
        "ok": True,
        "user": {
            "id": int(auth.owner.id or 0),
            "username": auth.owner.username,
            "role": auth.owner.role,
        },
        "api_key": _serialize_key(auth.api_key, owner=auth.owner, proxy=auth.proxy),
    }
    _record_api_access(
        request=request,
        status_code=status.HTTP_200_OK,
        auth=auth,
        target_url=str(auth.api_key.target_url or "").strip(),
    )
    return response_payload


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


def _normalize_anthropic_content(value: Any) -> Any:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value)

    normalized_blocks: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized_blocks.append({"type": "text", "text": text})
            continue
        if not isinstance(item, dict):
            text = str(item).strip()
            if text:
                normalized_blocks.append({"type": "text", "text": text})
            continue

        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                normalized_blocks.append({"type": "text", "text": text})
            continue

        if item_type == "image":
            source = item.get("source")
            image_url = ""
            if isinstance(source, dict):
                source_type = str(source.get("type") or "").strip().lower()
                if source_type == "url":
                    image_url = str(source.get("url") or "").strip()
                elif source_type == "base64":
                    media_type = str(source.get("media_type") or "application/octet-stream").strip()
                    data = str(source.get("data") or "").strip()
                    if data:
                        image_url = f"data:{media_type};base64,{data}"
            if image_url:
                normalized_blocks.append({"type": "image_url", "image_url": {"url": image_url}})
            continue

        text = str(item.get("text") or "").strip()
        if text:
            normalized_blocks.append({"type": "text", "text": text})
            continue

        try:
            serialized = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            serialized = str(item)
        if serialized:
            normalized_blocks.append({"type": "text", "text": serialized})

    if not normalized_blocks:
        return ""
    if len(normalized_blocks) == 1 and normalized_blocks[0].get("type") == "text":
        return normalized_blocks[0].get("text") or ""
    return normalized_blocks


def _anthropic_to_openai_messages(
    messages: Any,
    *,
    system: Any = None,
) -> list[dict[str, Any]]:
    openai_messages: list[dict[str, Any]] = []

    system_content = _normalize_anthropic_content(system)
    if system_content not in (None, ""):
        openai_messages.append({"role": "system", "content": system_content})

    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant", "system"}:
            continue
        content = _normalize_anthropic_content(item.get("content"))
        if content in (None, ""):
            continue
        openai_messages.append({"role": role, "content": content})

    return openai_messages


def _anthropic_stop_reason_from_openai(finish_reason: str | None) -> str:
    normalized = str(finish_reason or "").strip().lower()
    if normalized == "length":
        return "max_tokens"
    if normalized == "tool_calls":
        return "tool_use"
    return "end_turn"


def _build_anthropic_message_response(
    *,
    message_id: str,
    model: str,
    text: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, int(input_tokens or 0)),
            "output_tokens": max(0, int(output_tokens or 0)),
        },
    }


def _parse_openai_response_json(response: Response) -> dict[str, Any] | None:
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        body_text = body.decode("utf-8", errors="ignore")
    else:
        body_text = str(body or "")
    if not body_text:
        return None
    try:
        parsed = json.loads(body_text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _convert_openai_response_to_anthropic(response: Response, *, requested_model: str) -> Response:
    response_json = _parse_openai_response_json(response)
    if not response_json:
        return _build_anthropic_error_response(
            "上游响应不是有效 JSON",
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_type="api_error",
        )

    if response.status_code >= 400 or response_json.get("error"):
        error_payload = response_json.get("error")
        error_payload = error_payload if isinstance(error_payload, dict) else {}
        return _build_anthropic_error_response(
            str(error_payload.get("message") or "请求失败"),
            status_code=int(response.status_code or status.HTTP_502_BAD_GATEWAY),
        )

    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return _build_anthropic_error_response(
            "上游未返回有效 choices",
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_type="api_error",
        )

    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message_payload = first_choice.get("message")
    message_payload = message_payload if isinstance(message_payload, dict) else {}
    content_text = _extract_message_text_content(message_payload.get("content"))
    usage = response_json.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    finish_reason = str(first_choice.get("finish_reason") or "").strip() or "stop"
    message_id = str(response_json.get("id") or f"msg_{uuid.uuid4().hex}")
    model = str(response_json.get("model") or requested_model or "")

    return JSONResponse(
        status_code=int(response.status_code or status.HTTP_200_OK),
        content=_build_anthropic_message_response(
            message_id=message_id,
            model=model,
            text=content_text,
            input_tokens=_normalize_non_negative_int(usage.get("prompt_tokens", 0), field_name="prompt_tokens"),
            output_tokens=_normalize_non_negative_int(usage.get("completion_tokens", 0), field_name="completion_tokens"),
            stop_reason=_anthropic_stop_reason_from_openai(finish_reason),
        ),
    )


def _iter_sse_payloads(chunks: Any):
    buffer = ""
    data_lines: list[str] = []

    for raw_chunk in chunks:
        text = raw_chunk.decode("utf-8", errors="ignore") if isinstance(raw_chunk, bytes) else str(raw_chunk)
        if not text:
            continue
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())

    if data_lines:
        yield "\n".join(data_lines)


def _wrap_openai_stream_as_anthropic(response: StreamingResponse, *, requested_model: str) -> StreamingResponse:
    def anthropic_stream():
        message_id = f"msg_{uuid.uuid4().hex}"
        model = requested_model
        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"
        started = False
        block_started = False
        content_sent = False

        for payload_text in _iter_sse_payloads(response.body_iterator):
            if not payload_text:
                continue
            if payload_text == "[DONE]":
                break

            try:
                chunk = json.loads(payload_text)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue

            error_payload = chunk.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "请求失败")
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': message}}, ensure_ascii=False)}\n\n"
                return

            if not started:
                started = True
                chunk_model = str(chunk.get("model") or requested_model or "").strip()
                if chunk_model:
                    model = chunk_model
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    input_tokens = _normalize_non_negative_int(usage.get("prompt_tokens", 0), field_name="prompt_tokens")
                    output_tokens = _normalize_non_negative_int(usage.get("completion_tokens", 0), field_name="completion_tokens")
                yield (
                    "event: message_start\n"
                    f"data: {json.dumps({'type': 'message_start', 'message': _build_anthropic_message_response(message_id=message_id, model=model, text='', input_tokens=input_tokens, output_tokens=0, stop_reason=None)}, ensure_ascii=False)}\n\n"
                )

            choices = chunk.get("choices")
            choices = choices if isinstance(choices, list) else []
            first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            delta = first_choice.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            finish_reason = str(first_choice.get("finish_reason") or "").strip()
            delta_text = _extract_message_text_content(delta.get("content"))
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                input_tokens = _normalize_non_negative_int(usage.get("prompt_tokens", input_tokens), field_name="prompt_tokens")
                output_tokens = _normalize_non_negative_int(usage.get("completion_tokens", output_tokens), field_name="completion_tokens")

            if delta_text:
                if not block_started:
                    block_started = True
                    yield "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
                content_sent = True
                yield (
                    "event: content_block_delta\n"
                    f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_text}}, ensure_ascii=False)}\n\n"
                )

            if finish_reason:
                stop_reason = _anthropic_stop_reason_from_openai(finish_reason)

        if not started:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': '上游流式响应为空'}}, ensure_ascii=False)}\n\n"
            return

        if not block_started:
            block_started = True
            yield "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
        if block_started or content_sent:
            yield "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
        yield (
            "event: message_delta\n"
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}}, ensure_ascii=False)}\n\n"
        )
        yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    return StreamingResponse(
        anthropic_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _json_deep_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return value


def _build_responses_usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": 0,
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": 0,
        },
        "total_tokens": input_tokens + output_tokens,
    }


def _build_responses_output_message(
    *,
    item_id: str,
    text: str,
    status_text: str = "completed",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": status_text,
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": str(text or ""),
                "annotations": [],
            }
        ],
    }


def _build_responses_response_payload(
    *,
    response_id: str,
    model: str,
    instructions: str | None,
    previous_response_id: str | None,
    metadata: dict[str, Any] | None,
    store: bool,
    temperature: Any,
    top_p: Any,
    max_output_tokens: Any,
    tools: Any,
    tool_choice: Any,
    created_at: int,
    status_text: str,
    output_item_id: str = "",
    output_text: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    include_output: bool = True,
) -> dict[str, Any]:
    payload = {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at or time.time()),
        "status": status_text,
        "error": None,
        "incomplete_details": None,
        "instructions": instructions,
        "max_output_tokens": max_output_tokens,
        "model": str(model or "").strip(),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "output": [],
        "parallel_tool_calls": False,
        "previous_response_id": previous_response_id,
        "reasoning": {
            "effort": None,
            "summary": None,
        },
        "store": bool(store),
        "temperature": temperature if temperature is not None else 1,
        "text": {
            "format": {
                "type": "text",
            }
        },
        "tool_choice": tool_choice if tool_choice is not None else "auto",
        "tools": tools if isinstance(tools, list) else [],
        "top_p": top_p if top_p is not None else 1,
        "truncation": "disabled",
        "usage": _build_responses_usage(prompt_tokens, completion_tokens),
        "user": None,
        "output_text": str(output_text or ""),
    }
    if include_output:
        payload["output"] = [
            _build_responses_output_message(
                item_id=output_item_id or f"msg_{uuid.uuid4().hex}",
                text=output_text,
                status_text="completed" if status_text == "completed" else status_text,
            )
        ]
    return payload


def _store_response_record(record: dict[str, Any]) -> None:
    response_id = str(record.get("id") or "").strip()
    if not response_id:
        return
    with _responses_store_lock:
        _responses_store[response_id] = _json_deep_copy(record)


def _get_response_record(response_id: str, *, api_key_id: int | None = None) -> dict[str, Any] | None:
    normalized_id = str(response_id or "").strip()
    if not normalized_id:
        return None
    with _responses_store_lock:
        record = _responses_store.get(normalized_id)
    if not isinstance(record, dict):
        return None
    if api_key_id is not None and int(record.get("api_key_id") or 0) != int(api_key_id or 0):
        return None
    return _json_deep_copy(record)


def _delete_response_record(response_id: str, *, api_key_id: int | None = None) -> dict[str, Any] | None:
    normalized_id = str(response_id or "").strip()
    if not normalized_id:
        return None
    with _responses_store_lock:
        record = _responses_store.get(normalized_id)
        if not isinstance(record, dict):
            return None
        if api_key_id is not None and int(record.get("api_key_id") or 0) != int(api_key_id or 0):
            return None
        removed = _responses_store.pop(normalized_id, None)
    return _json_deep_copy(removed) if isinstance(removed, dict) else None


def _normalize_responses_content_parts(content: Any) -> tuple[list[dict[str, Any]], Any]:
    response_parts: list[dict[str, Any]] = []
    openai_parts: list[dict[str, Any]] = []
    text_parts: list[str] = []
    has_non_text = False

    def add_text(text: Any) -> None:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        response_parts.append({"type": "input_text", "text": normalized_text})
        openai_parts.append({"type": "text", "text": normalized_text})
        text_parts.append(normalized_text)

    def add_image(url: Any, detail: Any = None) -> None:
        nonlocal has_non_text
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return
        has_non_text = True
        response_part: dict[str, Any] = {
            "type": "input_image",
            "image_url": normalized_url,
        }
        if detail not in (None, ""):
            response_part["detail"] = str(detail)
        response_parts.append(response_part)
        openai_parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": normalized_url,
                },
            }
        )

    if content in (None, ""):
        return [], ""

    if isinstance(content, str):
        add_text(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                add_text(item)
                continue
            if not isinstance(item, dict):
                add_text(item)
                continue

            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"input_text", "text", "output_text"}:
                add_text(item.get("text"))
                continue

            if item_type in {"input_image", "image_url", "image"}:
                image_value = item.get("image_url")
                if isinstance(image_value, dict):
                    image_value = image_value.get("url")
                if item_type == "image":
                    image_value = image_value or item.get("url")
                add_image(image_value or item.get("url"), item.get("detail"))
                continue

            if item.get("text") not in (None, ""):
                add_text(item.get("text"))
                continue

            serialized = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            add_text(serialized)
    elif isinstance(content, dict):
        item_type = str(content.get("type") or "").strip().lower()
        if item_type in {"input_text", "text", "output_text"}:
            add_text(content.get("text"))
        elif item_type in {"input_image", "image_url", "image"}:
            image_value = content.get("image_url")
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            add_image(image_value or content.get("url"), content.get("detail"))
        elif "text" in content:
            add_text(content.get("text"))
        else:
            serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            add_text(serialized)
    else:
        add_text(content)

    if not response_parts:
        return [], ""
    if not has_non_text:
        return response_parts, "\n".join(text_parts)
    return response_parts, openai_parts


def _normalize_responses_message_item(item: Any, *, default_role: str = "user") -> tuple[dict[str, Any], dict[str, Any]] | None:
    role = default_role
    content = item

    if isinstance(item, dict):
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"input_text", "text", "output_text", "input_image", "image_url", "image"} and "content" not in item and "role" not in item:
            content = [item]
        else:
            role = str(item.get("role") or default_role).strip().lower() or default_role
            content = item.get("content")

    if role not in {"user", "assistant", "system", "developer"}:
        role = default_role

    response_content, openai_content = _normalize_responses_content_parts(content)
    if not response_content:
        return None

    response_item = {
        "type": "message",
        "role": role,
        "content": response_content,
    }
    openai_role = "system" if role == "developer" else role
    openai_message = {
        "role": openai_role,
        "content": openai_content,
    }
    return response_item, openai_message


def _responses_input_to_openai_messages(input_value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if input_value in (None, ""):
        return [], []

    input_items: list[dict[str, Any]] = []
    openai_messages: list[dict[str, Any]] = []

    def append_message(value: Any, *, default_role: str = "user") -> None:
        normalized = _normalize_responses_message_item(value, default_role=default_role)
        if not normalized:
            return
        response_item, openai_message = normalized
        input_items.append(response_item)
        openai_messages.append(openai_message)

    if isinstance(input_value, list):
        is_message_list = bool(input_value) and all(
            isinstance(item, dict) and (
                "role" in item or str(item.get("type") or "").strip().lower() == "message"
            )
            for item in input_value
        )
        if is_message_list:
            for item in input_value:
                append_message(item)
        else:
            append_message({"role": "user", "content": input_value})
        return input_items, openai_messages

    if isinstance(input_value, dict):
        item_type = str(input_value.get("type") or "").strip().lower()
        if "role" in input_value or item_type == "message":
            append_message(input_value)
        else:
            append_message({"role": "user", "content": [input_value]})
        return input_items, openai_messages

    append_message({"role": "user", "content": str(input_value)})
    return input_items, openai_messages


def _build_openai_error_from_exception(exc: HTTPException, *, code: str) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "request failed")
    else:
        message = str(detail or "request failed")
    return _build_openai_error_response(
        message,
        status_code=int(exc.status_code or status.HTTP_400_BAD_REQUEST),
        code=code,
    )


def _prepare_responses_request(
    *,
    auth: SKAuthContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON 对象")

    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model 不能为空")

    previous_response_id = str(payload.get("previous_response_id") or "").strip() or None
    previous_record = None
    history_messages: list[dict[str, Any]] = []
    if previous_response_id:
        previous_record = _get_response_record(previous_response_id, api_key_id=int(auth.api_key.id or 0))
        if not previous_record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="previous_response_id 不存在")
        history_messages = previous_record.get("conversation_messages") or []
        history_messages = history_messages if isinstance(history_messages, list) else []

    input_items, input_messages = _responses_input_to_openai_messages(payload.get("input"))
    instructions = str(payload.get("instructions") or "").strip() or None
    conversation_messages = [_json_deep_copy(item) for item in history_messages]
    conversation_messages.extend(_json_deep_copy(input_messages))

    openai_messages: list[dict[str, Any]] = []
    if instructions:
        openai_messages.append({"role": "system", "content": instructions})
    openai_messages.extend(_json_deep_copy(conversation_messages))

    if not openai_messages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="input 不能为空")

    openai_payload: dict[str, Any] = {
        "model": model,
        "messages": openai_messages,
        "stream": bool(payload.get("stream")),
    }
    if "max_output_tokens" in payload:
        openai_payload["max_completion_tokens"] = payload.get("max_output_tokens")
    elif "max_tokens" in payload:
        openai_payload["max_tokens"] = payload.get("max_tokens")
    if "temperature" in payload:
        openai_payload["temperature"] = payload.get("temperature")
    if "top_p" in payload:
        openai_payload["top_p"] = payload.get("top_p")
    if "target_url" in payload:
        openai_payload["target_url"] = payload.get("target_url")
    if "stop" in payload:
        openai_payload["stop"] = payload.get("stop")

    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "requested_payload": payload,
        "openai_payload": openai_payload,
        "input_items": input_items,
        "conversation_messages": conversation_messages,
        "instructions": instructions,
        "previous_response_id": previous_response_id,
        "model": model,
        "stream": bool(payload.get("stream")),
        "metadata": metadata,
        "store": bool(payload.get("store", True)),
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "max_output_tokens": payload.get("max_output_tokens"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
    }


def _convert_openai_response_to_responses(
    response: Response,
    *,
    auth: SKAuthContext,
    request_context: dict[str, Any],
) -> Response:
    response_json = _parse_openai_response_json(response)
    if not response_json:
        return _build_openai_error_response(
            "上游响应不是有效 JSON",
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="responses_invalid_json",
        )

    if response.status_code >= 400 or response_json.get("error"):
        return JSONResponse(
            status_code=int(response.status_code or status.HTTP_502_BAD_GATEWAY),
            content=response_json,
        )

    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return _build_openai_error_response(
            "上游未返回有效 choices",
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="responses_invalid_choices",
        )

    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message_payload = first_choice.get("message")
    message_payload = message_payload if isinstance(message_payload, dict) else {}
    output_text = _extract_message_text_content(message_payload.get("content"))
    prompt_tokens, completion_tokens = _resolve_usage_from_json(
        response_json,
        prompt_tokens_estimate=_estimate_chat_tokens(request_context.get("openai_payload", {}).get("messages") or []),
        completion_fallback=output_text,
    )

    response_id = f"resp_{uuid.uuid4().hex}"
    output_item_id = str(response_json.get("response_message_id") or f"msg_{uuid.uuid4().hex}")
    created_at = int(time.time())
    model = str(response_json.get("model") or request_context.get("model") or "")
    response_payload = _build_responses_response_payload(
        response_id=response_id,
        model=model,
        instructions=request_context.get("instructions"),
        previous_response_id=request_context.get("previous_response_id"),
        metadata=request_context.get("metadata"),
        store=bool(request_context.get("store")),
        temperature=request_context.get("temperature"),
        top_p=request_context.get("top_p"),
        max_output_tokens=request_context.get("max_output_tokens"),
        tools=request_context.get("tools"),
        tool_choice=request_context.get("tool_choice"),
        created_at=created_at,
        status_text="completed",
        output_item_id=output_item_id,
        output_text=output_text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    record = {
        "id": response_id,
        "api_key_id": int(auth.api_key.id or 0),
        "response": response_payload,
        "input_items": request_context.get("input_items") or [],
        "conversation_messages": [
            *_json_deep_copy(request_context.get("conversation_messages") or []),
            {"role": "assistant", "content": output_text},
        ],
        "created_at": created_at,
    }
    _store_response_record(record)
    return JSONResponse(
        status_code=int(response.status_code or status.HTTP_200_OK),
        content=response_payload,
    )


def _wrap_openai_stream_as_responses(
    response: StreamingResponse,
    *,
    auth: SKAuthContext,
    request_context: dict[str, Any],
) -> StreamingResponse:
    response_id = f"resp_{uuid.uuid4().hex}"
    output_item_id = f"msg_{uuid.uuid4().hex}"
    created_at = int(time.time())
    requested_model = str(request_context.get("model") or "")
    metadata = request_context.get("metadata")
    instructions = request_context.get("instructions")
    previous_response_id = request_context.get("previous_response_id")
    store = bool(request_context.get("store"))
    temperature = request_context.get("temperature")
    top_p = request_context.get("top_p")
    max_output_tokens = request_context.get("max_output_tokens")
    tools = request_context.get("tools")
    tool_choice = request_context.get("tool_choice")

    def responses_stream():
        model = requested_model
        accumulated_text = ""
        prompt_tokens = _estimate_chat_tokens(request_context.get("openai_payload", {}).get("messages") or [])
        completion_tokens = 0
        created_payload = _build_responses_response_payload(
            response_id=response_id,
            model=model,
            instructions=instructions,
            previous_response_id=previous_response_id,
            metadata=metadata,
            store=store,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            created_at=created_at,
            status_text="in_progress",
            include_output=False,
        )
        yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': created_payload}, ensure_ascii=False)}\n\n"
        yield f"event: response.in_progress\ndata: {json.dumps({'type': 'response.in_progress', 'response': created_payload}, ensure_ascii=False)}\n\n"
        yield (
            "event: response.output_item.added\n"
            f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': 0, 'item': {'id': output_item_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: response.content_part.added\n"
            f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': response_id, 'output_index': 0, 'item_id': output_item_id, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}}, ensure_ascii=False)}\n\n"
        )

        for payload_text in _iter_sse_payloads(response.body_iterator):
            if not payload_text:
                continue
            if payload_text == "[DONE]":
                break

            try:
                chunk = json.loads(payload_text)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue

            error_payload = chunk.get("error")
            if isinstance(error_payload, dict):
                error_message = str(error_payload.get("message") or "请求失败")
                failed_payload = {
                    **created_payload,
                    "status": "failed",
                    "error": {
                        "message": error_message,
                        "type": str(error_payload.get("type") or "api_error"),
                        "code": error_payload.get("code"),
                    },
                }
                yield f"event: response.failed\ndata: {json.dumps({'type': 'response.failed', 'response': failed_payload}, ensure_ascii=False)}\n\n"
                return

            chunk_model = str(chunk.get("model") or "").strip()
            if chunk_model:
                model = chunk_model
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                prompt_tokens = _normalize_non_negative_int(usage.get("prompt_tokens", prompt_tokens), field_name="prompt_tokens")
                completion_tokens = _normalize_non_negative_int(usage.get("completion_tokens", completion_tokens), field_name="completion_tokens")

            delta_text = _extract_chunk_text(chunk)
            if delta_text:
                accumulated_text += delta_text
                yield (
                    "event: response.output_text.delta\n"
                    f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': 0, 'item_id': output_item_id, 'content_index': 0, 'delta': delta_text}, ensure_ascii=False)}\n\n"
                )

        if completion_tokens <= 0 and accumulated_text:
            completion_tokens = _estimate_text_tokens(accumulated_text)

        completed_item = _build_responses_output_message(
            item_id=output_item_id,
            text=accumulated_text,
            status_text="completed",
        )
        response_payload = _build_responses_response_payload(
            response_id=response_id,
            model=model,
            instructions=instructions,
            previous_response_id=previous_response_id,
            metadata=metadata,
            store=store,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            created_at=created_at,
            status_text="completed",
            output_item_id=output_item_id,
            output_text=accumulated_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        _store_response_record(
            {
                "id": response_id,
                "api_key_id": int(auth.api_key.id or 0),
                "response": response_payload,
                "input_items": request_context.get("input_items") or [],
                "conversation_messages": [
                    *_json_deep_copy(request_context.get("conversation_messages") or []),
                    {"role": "assistant", "content": accumulated_text},
                ],
                "created_at": created_at,
            }
        )
        yield (
            "event: response.output_text.done\n"
            f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': response_id, 'output_index': 0, 'item_id': output_item_id, 'content_index': 0, 'text': accumulated_text}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: response.content_part.done\n"
            f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': response_id, 'output_index': 0, 'item_id': output_item_id, 'content_index': 0, 'part': completed_item['content'][0]}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: response.output_item.done\n"
            f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 0, 'item': completed_item}, ensure_ascii=False)}\n\n"
        )
        yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': response_payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        responses_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
def list_openai_models(
    request: Request,
    auth: SKAuthContext = Depends(get_sk_auth_context),
):
    started_at = time.monotonic()
    target_url = _normalize_chat_url(auth.api_key.target_url)
    try:
        if _is_official_chatgpt_target(target_url):
            response = _handle_official_chatgpt_models(auth, target_url)
        else:
            import requests

            models_url = _derive_models_url(target_url)
            upstream_response = requests.get(
                models_url,
                headers=_build_upstream_headers(auth),
                proxies=build_requests_proxy_config(auth.proxy_url),
                timeout=60,
            )
            success = upstream_response.status_code < 400
            error_text = "" if success else str(upstream_response.text or "")[:500]
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

            response_content_type = str(upstream_response.headers.get("Content-Type", "") or "")
            if "json" in response_content_type.lower():
                try:
                    response = JSONResponse(content=upstream_response.json(), status_code=upstream_response.status_code)
                except ValueError:
                    response = Response(
                        content=str(upstream_response.text or ""),
                        status_code=upstream_response.status_code,
                        media_type=response_content_type or "text/plain",
                    )
            else:
                response = Response(
                    content=str(upstream_response.text or ""),
                    status_code=upstream_response.status_code,
                    media_type=response_content_type or "text/plain",
                )

        _record_api_access(
            request=request,
            status_code=int(response.status_code or status.HTTP_200_OK),
            auth=auth,
            model="models",
            target_url=target_url,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return response
    except HTTPException as exc:
        _record_api_access(
            request=request,
            status_code=int(exc.status_code or status.HTTP_400_BAD_REQUEST),
            auth=auth,
            model="models",
            target_url=target_url,
            error=str(exc.detail or ""),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise


def _handle_openai_chat_completions_payload(
    *,
    auth: SKAuthContext,
    payload: dict[str, Any],
) -> Response:
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


@router.post("/sk/chat/completions")
@openai_router.post("/chat/completions")
async def sk_chat_completions(
    request: Request,
    auth: SKAuthContext = Depends(get_sk_auth_context),
):
    started_at = time.monotonic()
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON") from exc

    model = str(payload.get("model") or "").strip()
    target_url = _normalize_chat_url(payload.get("target_url") or auth.api_key.target_url or "")
    try:
        response = _handle_openai_chat_completions_payload(auth=auth, payload=payload)
        _record_api_access(
            request=request,
            status_code=int(response.status_code or status.HTTP_200_OK),
            auth=auth,
            model=model,
            target_url=target_url,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return response
    except HTTPException as exc:
        _record_api_access(
            request=request,
            status_code=int(exc.status_code or status.HTTP_400_BAD_REQUEST),
            auth=auth,
            model=model,
            target_url=target_url,
            error=str(exc.detail or ""),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise


@router.post("/sk/anthropic/messages")
@openai_router.post("/messages")
@anthropic_apps_router.post("/messages")
@anthropic_apps_router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_anthropic_error_response(
            str(exc.detail or "鉴权失败"),
            status_code=int(exc.status_code or status.HTTP_401_UNAUTHORIZED),
        )

    try:
        payload = await request.json()
    except Exception:
        return _build_anthropic_error_response(
            "请求体必须是 JSON",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not isinstance(payload, dict):
        return _build_anthropic_error_response(
            "请求体必须是 JSON 对象",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    model = str(payload.get("model") or "").strip()
    if not model:
        return _build_anthropic_error_response(
            "model 不能为空",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    openai_messages = _anthropic_to_openai_messages(
        payload.get("messages") or [],
        system=payload.get("system"),
    )
    if not openai_messages:
        return _build_anthropic_error_response(
            "messages 不能为空",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    openai_payload: dict[str, Any] = {
        "model": model,
        "messages": openai_messages,
        "stream": bool(payload.get("stream")),
    }
    if "max_tokens" in payload:
        openai_payload["max_tokens"] = payload.get("max_tokens")
    if "temperature" in payload:
        openai_payload["temperature"] = payload.get("temperature")
    if "top_p" in payload:
        openai_payload["top_p"] = payload.get("top_p")
    if "target_url" in payload:
        openai_payload["target_url"] = payload.get("target_url")
    if "stop_sequences" in payload:
        openai_payload["stop"] = payload.get("stop_sequences")

    try:
        openai_response = _handle_openai_chat_completions_payload(auth=auth, payload=openai_payload)
    except HTTPException as exc:
        return _build_anthropic_error_response(
            str(exc.detail or "请求失败"),
            status_code=int(exc.status_code or status.HTTP_400_BAD_REQUEST),
        )

    if bool(payload.get("stream")):
        if isinstance(openai_response, StreamingResponse):
            return _wrap_openai_stream_as_anthropic(openai_response, requested_model=model)
        return _convert_openai_response_to_anthropic(openai_response, requested_model=model)

    return _convert_openai_response_to_anthropic(openai_response, requested_model=model)


@openai_router.post("/responses")
async def openai_responses(
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_auth_error")

    try:
        payload = await request.json()
    except Exception:
        return _build_openai_error_response(
            "请求体必须是 JSON",
            status_code=status.HTTP_400_BAD_REQUEST,
            code="responses_invalid_json",
        )

    try:
        request_context = _prepare_responses_request(auth=auth, payload=payload)
        openai_response = _handle_openai_chat_completions_payload(
            auth=auth,
            payload=request_context["openai_payload"],
        )
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_request_error")

    if request_context.get("stream"):
        if isinstance(openai_response, StreamingResponse):
            return _wrap_openai_stream_as_responses(
                openai_response,
                auth=auth,
                request_context=request_context,
            )
        return _convert_openai_response_to_responses(
            openai_response,
            auth=auth,
            request_context=request_context,
        )

    return _convert_openai_response_to_responses(
        openai_response,
        auth=auth,
        request_context=request_context,
    )


@openai_router.post("/responses/input_tokens")
async def openai_responses_input_tokens(
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_auth_error")

    try:
        payload = await request.json()
    except Exception:
        return _build_openai_error_response(
            "请求体必须是 JSON",
            status_code=status.HTTP_400_BAD_REQUEST,
            code="responses_invalid_json",
        )

    try:
        request_context = _prepare_responses_request(auth=auth, payload=payload)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_request_error")

    input_tokens = _estimate_chat_tokens(request_context.get("openai_payload", {}).get("messages") or [])
    return {
        "object": "response.input_tokens",
        "model": request_context.get("model"),
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": 0,
        },
    }


@openai_router.get("/responses/{response_id}/input_items")
def openai_response_input_items(
    response_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_auth_error")

    record = _get_response_record(response_id, api_key_id=int(auth.api_key.id or 0))
    if not record:
        return _build_openai_error_response(
            "response 不存在",
            status_code=status.HTTP_404_NOT_FOUND,
            code="response_not_found",
        )

    items = record.get("input_items") or []
    items = items if isinstance(items, list) else []
    return {
        "object": "list",
        "data": items,
        "first_id": 0,
        "last_id": max(0, len(items) - 1),
        "has_more": False,
    }


@openai_router.get("/responses/{response_id}")
def get_openai_response(
    response_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_auth_error")

    record = _get_response_record(response_id, api_key_id=int(auth.api_key.id or 0))
    if not record:
        return _build_openai_error_response(
            "response 不存在",
            status_code=status.HTTP_404_NOT_FOUND,
            code="response_not_found",
        )

    response_payload = record.get("response")
    response_payload = response_payload if isinstance(response_payload, dict) else {}
    return JSONResponse(content=response_payload)


@openai_router.delete("/responses/{response_id}")
def delete_openai_response(
    response_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    try:
        auth = get_sk_auth_context_from_request(request, session)
    except HTTPException as exc:
        return _build_openai_error_from_exception(exc, code="responses_auth_error")

    record = _delete_response_record(response_id, api_key_id=int(auth.api_key.id or 0))
    if not record:
        return _build_openai_error_response(
            "response 不存在",
            status_code=status.HTTP_404_NOT_FOUND,
            code="response_not_found",
        )

    return {
        "id": str(response_id or ""),
        "object": "response.deleted",
        "deleted": True,
    }
