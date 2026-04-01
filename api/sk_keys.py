import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlmodel import Session, select

from core.db import ProxyModel, SKApiKeyModel, SKApiKeyUsageLog, UserModel, get_session
from core.proxy_utils import build_requests_proxy_config, normalize_proxy_url
from core.security import (
    generate_sk_api_key,
    get_current_user,
    hash_sk_api_key,
)

router = APIRouter(tags=["sk"])
_sk_bearer_scheme = HTTPBearer(auto_error=False)


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


def _normalize_chat_url(target_url: str) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""

    parts = urlsplit(value)
    path = str(parts.path or "").rstrip("/")
    if path.endswith("/chat/completions"):
        return value

    next_path = f"{path}/chat/completions" if path else "/chat/completions"
    return urlunsplit(parts._replace(path=next_path))


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


@router.post("/sk/chat/completions")
async def sk_chat_completions(
    request: Request,
    auth: SKAuthContext = Depends(get_sk_auth_context),
    session: Session = Depends(get_session),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须是 JSON 对象")
    if bool(payload.get("stream")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前 SK 网关仅支持非流式请求")

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="messages 不能为空")

    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="model 不能为空")

    target_url = _normalize_chat_url(auth.api_key.target_url or payload.get("target_url") or "")
    if not target_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未配置 target_url")

    prompt_tokens_estimate = _estimate_chat_tokens(messages)
    planned_completion_tokens = _normalize_non_negative_int(
        payload.get("max_tokens", 0),
        field_name="max_tokens",
    )
    _ensure_quota(auth.api_key, prompt_tokens_estimate + planned_completion_tokens)

    request_payload = dict(payload)
    request_payload.pop("target_url", None)
    request_payload["model"] = model
    request_payload["messages"] = messages
    request_payload["stream"] = False

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    upstream_api_key = str(auth.api_key.upstream_api_key or "").strip()
    if upstream_api_key:
        headers["Authorization"] = f"Bearer {upstream_api_key}"

    import requests

    response = requests.post(
        target_url,
        json=request_payload,
        headers=headers,
        proxies=build_requests_proxy_config(auth.proxy_url),
        timeout=180,
    )

    response_content_type = str(response.headers.get("Content-Type", "") or "")
    response_json: dict[str, Any] | None = None
    response_text = str(response.text or "")
    if "json" in response_content_type.lower():
        try:
            response_json = response.json()
        except ValueError:
            response_json = None

    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    prompt_tokens = (
        _normalize_non_negative_int(usage.get("prompt_tokens", 0), field_name="prompt_tokens")
        if isinstance(usage, dict)
        else prompt_tokens_estimate
    )
    completion_tokens = (
        _normalize_non_negative_int(usage.get("completion_tokens", 0), field_name="completion_tokens")
        if isinstance(usage, dict)
        else (0 if not success else _estimate_text_tokens(response_text))
    )

    success = response.status_code < 400
    _record_usage(
        session,
        auth.api_key,
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
