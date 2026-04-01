import csv
import io
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from core.base_platform import Account, AccountStatus, RegisterConfig
from core.config_store import config_store
from core.db import AccountModel, engine, get_session
from core.registry import get, load_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchCheckRequest(BaseModel):
    ids: list[int]


class ChatGPTConversationRequest(BaseModel):
    prompt: str
    mode: str = "official"
    stream: bool = True
    conversation_id: Optional[str] = None
    parent_message_id: Optional[str] = None
    proxy: Optional[str] = None
    target_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    messages: list[dict] = Field(default_factory=list)


class ChatGPTModelsRequest(BaseModel):
    proxy: Optional[str] = None
    target_url: Optional[str] = None


DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _normalize_account_ids(ids: list[int], *, max_size: int = 200) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()

    for raw_id in ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)

    if not normalized:
        raise HTTPException(400, "账号 ID 列表不能为空")
    if len(normalized) > max_size:
        raise HTTPException(400, f"单次最多检测 {max_size} 个账号")
    return normalized


def _parse_account_extra(extra_json: str) -> dict:
    try:
        parsed = json.loads(extra_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _exception_message(exc: Exception, fallback: str) -> str:
    text = str(exc or "").strip()
    if text:
        return text
    exc_type = type(exc).__name__.strip()
    if exc_type and exc_type != "Exception":
        return f"{fallback}: {exc_type}"
    return fallback


def _coerce_account_status(status: str) -> AccountStatus:
    try:
        return AccountStatus(status)
    except ValueError:
        return AccountStatus.REGISTERED


def _build_runtime_account(acc: AccountModel) -> Account:
    return Account(
        platform=acc.platform,
        email=acc.email,
        password=acc.password,
        user_id=acc.user_id,
        region=acc.region,
        token=acc.token,
        status=_coerce_account_status(acc.status),
        trial_end_time=acc.trial_end_time,
        extra=_parse_account_extra(acc.extra_json),
        created_at=int((acc.created_at or datetime.now(timezone.utc)).timestamp()),
    )


def _get_chatgpt_account_or_404(account_id: int, session: Session) -> AccountModel:
    acc = session.get(AccountModel, account_id)
    if not acc or acc.platform != "chatgpt":
        raise HTTPException(404, "账号不存在")
    return acc


def _build_chatgpt_message_account(acc: AccountModel, extra: dict) -> SimpleNamespace:
    return SimpleNamespace(
        email=acc.email,
        access_token=str(extra.get("access_token") or acc.token or "").strip(),
        refresh_token=str(extra.get("refresh_token") or "").strip(),
        id_token=str(extra.get("id_token") or "").strip(),
        session_token=str(extra.get("session_token") or "").strip(),
        client_id=str(extra.get("client_id") or DEFAULT_CHATGPT_CLIENT_ID).strip() or DEFAULT_CHATGPT_CLIENT_ID,
        cookies=str(extra.get("cookies") or "").strip(),
    )


def _resolve_chatgpt_proxy(acc: AccountModel, extra: dict, preferred_proxy: str = "") -> str:
    from core.proxy_pool import proxy_pool

    proxy = str(preferred_proxy or "").strip()
    if proxy:
        return proxy

    proxy = str(extra.get("test_proxy") or config_store.get("chatgpt_test_proxy", "") or "").strip()
    if proxy:
        return proxy

    proxy = proxy_pool.get_next(region=acc.region or "") or proxy_pool.get_next() or ""
    return str(proxy or "").strip()


def _report_chatgpt_proxy_result(proxy: str, *, ok: bool, invalid: bool) -> None:
    from core.proxy_pool import proxy_pool

    normalized_proxy = str(proxy or "").strip()
    if not normalized_proxy:
        return
    if ok or invalid:
        proxy_pool.report_success(normalized_proxy)
    else:
        proxy_pool.report_fail(normalized_proxy)


def _apply_chatgpt_message_result(
    acc: AccountModel,
    extra: dict,
    *,
    updated_access_token: str = "",
    updated_refresh_token: str = "",
    invalid: bool = False,
) -> None:
    changed = False

    if updated_access_token:
        extra["access_token"] = updated_access_token
        acc.token = updated_access_token
        changed = True
    if updated_refresh_token:
        extra["refresh_token"] = updated_refresh_token
        changed = True
    if invalid and acc.status != AccountStatus.INVALID.value:
        acc.status = AccountStatus.INVALID.value
        changed = True

    if changed:
        acc.extra_json = json.dumps(extra, ensure_ascii=False)
    acc.updated_at = datetime.now(timezone.utc)


def _persist_chatgpt_message_result(
    account_id: int,
    *,
    updated_access_token: str = "",
    updated_refresh_token: str = "",
    invalid: bool = False,
) -> None:
    with Session(engine) as session:
        acc = session.get(AccountModel, account_id)
        if not acc or acc.platform != "chatgpt":
            return
        extra = _parse_account_extra(acc.extra_json)
        _apply_chatgpt_message_result(
            acc,
            extra,
            updated_access_token=updated_access_token,
            updated_refresh_token=updated_refresh_token,
            invalid=invalid,
        )
        session.add(acc)
        session.commit()


def _encode_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalize_chat_messages(messages: list[dict], prompt: str) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"system", "user", "assistant"} or not content:
            continue
        normalized.append({"role": role, "content": content})

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return normalized
    if normalized and normalized[-1]["role"] == "user" and normalized[-1]["content"] == prompt_text:
        return normalized
    normalized.append({"role": "user", "content": prompt_text})
    return normalized


def _normalize_openai_chat_url(target_url: str) -> str:
    raw = str(target_url or "").strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    path = str(parts.path or "").rstrip("/")
    if path.endswith("/chat/completions"):
        return raw

    next_path = f"{path}/chat/completions" if path else "/chat/completions"
    if path.endswith("/v1"):
        next_path = f"{path}/chat/completions"
    return urlunsplit(parts._replace(path=next_path))


def _extract_openai_message_text(choice: dict) -> str:
    if not isinstance(choice, dict):
        return ""

    for field in ("message", "delta"):
        payload = choice.get(field)
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                    continue
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    parts.append(text)
            if parts:
                return "".join(parts)

    text = choice.get("text")
    if isinstance(text, str):
        return text.strip()
    return ""


def _extract_openai_response_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    choices = payload.get("choices")
    if isinstance(choices, list):
        parts: list[str] = []
        for choice in choices:
            text = _extract_openai_message_text(choice)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()
    return ""


def _stream_openai_compatible_chat(
    *,
    target_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    proxy: str,
    stream: bool = True,
) -> StreamingResponse:
    import requests

    from core.proxy_utils import build_requests_proxy_config

    normalized_url = _normalize_openai_chat_url(target_url)
    if not normalized_url:
        raise HTTPException(400, "自定义对话 URL 不能为空")

    def event_stream():
        accumulated = ""
        try:
            yield _encode_sse(
                "meta",
                {
                    "used_proxy": proxy,
                    "target_url": normalized_url,
                    "transport": "openai_compatible",
                    "model": model,
                    "request_mode": "stream" if stream else "sync",
                    "chain": "openai_compatible_stream" if stream else "openai_compatible_sync",
                },
            )

            request_json = {
                "model": model,
                "messages": messages,
                "stream": bool(stream),
            }
            request_headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                **({"Authorization": f"Bearer {api_key}"} if str(api_key or "").strip() else {}),
            }
            request_kwargs = {
                "json": request_json,
                "headers": request_headers,
                "proxies": build_requests_proxy_config(proxy),
                "timeout": 180,
            }
            if stream:
                request_kwargs["stream"] = True

            response = requests.post(normalized_url, **request_kwargs)

            with response:
                if response.status_code >= 400:
                    _report_chatgpt_proxy_result(proxy, ok=False, invalid=False)
                    yield _encode_sse(
                        "error",
                        {
                            "ok": False,
                            "invalid": False,
                            "message": f"自定义接口调用失败: HTTP {response.status_code}",
                            "response_text": response.text[:2000],
                            "used_proxy": proxy,
                            "target_url": normalized_url,
                            "model": model,
                            "request_mode": "stream" if stream else "sync",
                            "chain": "openai_compatible_stream" if stream else "openai_compatible_sync",
                        },
                    )
                    return

                if not stream:
                    response_text = ""
                    try:
                        response_text = _extract_openai_response_text(response.json())
                    except Exception:
                        response_text = ""
                    if not response_text:
                        response_text = str(response.text or "").strip()

                    if not response_text:
                        _report_chatgpt_proxy_result(proxy, ok=False, invalid=False)
                        yield _encode_sse(
                            "error",
                            {
                                "ok": False,
                                "invalid": False,
                                "message": "自定义接口已返回，但没有解析到回复内容",
                                "used_proxy": proxy,
                                "target_url": normalized_url,
                                "model": model,
                                "request_mode": "sync",
                                "chain": "openai_compatible_sync",
                            },
                        )
                        return

                    _report_chatgpt_proxy_result(proxy, ok=True, invalid=False)
                    yield _encode_sse(
                        "done",
                        {
                            "ok": True,
                            "invalid": False,
                            "message": f"自定义接口回复成功: {response_text[:80]}",
                            "response_excerpt": response_text[:200],
                            "response_text": response_text,
                            "used_proxy": proxy,
                            "target_url": normalized_url,
                            "model": model,
                            "request_mode": "sync",
                            "chain": "openai_compatible_sync",
                        },
                    )
                    return

                for raw_line in response.iter_lines(decode_unicode=True):
                    line = str(raw_line or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    if payload == "[DONE]":
                        break

                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue

                    delta = ""
                    choices = data.get("choices")
                    if isinstance(choices, list) and choices:
                        delta = _extract_openai_message_text(choices[0])

                    if not delta:
                        continue

                    accumulated += delta
                    yield _encode_sse(
                        "delta",
                        {
                            "delta": delta,
                            "used_proxy": proxy,
                            "target_url": normalized_url,
                            "model": model,
                            "request_mode": "stream",
                            "chain": "openai_compatible_stream",
                        },
                    )

            if not accumulated:
                _report_chatgpt_proxy_result(proxy, ok=False, invalid=False)
                yield _encode_sse(
                    "error",
                    {
                        "ok": False,
                        "invalid": False,
                        "message": "自定义接口已返回，但没有解析到回复内容",
                        "used_proxy": proxy,
                        "target_url": normalized_url,
                        "model": model,
                    },
                )
                return

            _report_chatgpt_proxy_result(proxy, ok=True, invalid=False)
            yield _encode_sse(
                "done",
                {
                    "ok": True,
                    "invalid": False,
                    "message": f"自定义接口回复成功: {accumulated[:80]}",
                    "response_excerpt": accumulated[:200],
                    "response_text": accumulated,
                    "used_proxy": proxy,
                    "target_url": normalized_url,
                    "model": model,
                    "request_mode": "stream",
                    "chain": "openai_compatible_stream",
                },
            )
        except Exception as e:
            _report_chatgpt_proxy_result(proxy, ok=False, invalid=False)
            yield _encode_sse(
                "error",
                {
                    "ok": False,
                    "invalid": False,
                    "message": str(e) or "自定义接口对话失败",
                    "used_proxy": proxy,
                    "target_url": normalized_url,
                    "model": model,
                    "request_mode": "stream" if stream else "sync",
                    "chain": "openai_compatible_stream" if stream else "openai_compatible_sync",
                },
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


def _iter_official_chat_chunks(
    account: SimpleNamespace,
    *,
    proxy: str,
    prompt: str,
    model: str,
    conversation_id: str,
    parent_message_id: str,
    target_url: str,
    stream: bool,
) -> Iterator[dict[str, dict]]:
    from platforms.chatgpt.message_tester import send_chat_message, stream_chat_message

    normalized_model = str(model or "auto").strip() or "auto"
    normalized_conversation_id = str(conversation_id or "").strip()
    normalized_parent_message_id = str(parent_message_id or "").strip()
    normalized_target_url = str(target_url or "").strip()
    if stream:
        yield from stream_chat_message(
            account,
            proxy=proxy,
            prompt=prompt,
            model=normalized_model,
            conversation_id=normalized_conversation_id,
            parent_message_id=normalized_parent_message_id,
            target_url=normalized_target_url,
        )
        return

    yield {
        "event": "meta",
        "data": {
            "used_proxy": proxy,
            "model": normalized_model,
            "conversation_id": normalized_conversation_id,
            "parent_message_id": normalized_parent_message_id,
            "target_url": normalized_target_url,
            "chain": "send_chat_message",
            "shared_test_flow": True,
            "request_mode": "sync",
        },
    }

    result = send_chat_message(
        account,
        proxy=proxy,
        prompt=prompt,
        model=normalized_model,
        conversation_id=normalized_conversation_id,
        parent_message_id=normalized_parent_message_id,
        target_url=normalized_target_url,
    )
    yield {
        "event": "done" if result.ok else "error",
        "data": {
            "ok": result.ok,
            "invalid": result.invalid,
            "message": result.message,
            "response_excerpt": result.response_excerpt,
            "response_text": result.response_text,
            "conversation_id": result.conversation_id,
            "response_message_id": result.response_message_id,
            "used_proxy": result.used_proxy or proxy,
            "model": result.model or normalized_model,
            "updated_access_token": result.updated_access_token,
            "updated_refresh_token": result.updated_refresh_token,
            "target_url": normalized_target_url,
            "chain": "send_chat_message",
            "shared_test_flow": True,
            "request_mode": "sync",
        },
    }


def _check_account_validity(acc: AccountModel, *, config: RegisterConfig | None = None) -> dict:
    if acc.platform == "chatgpt":
        from platforms.chatgpt.message_tester import send_test_message

        extra = _parse_account_extra(acc.extra_json)
        proxy = _resolve_chatgpt_proxy(acc, extra)
        if not proxy:
            return {
                "id": acc.id,
                "platform": acc.platform,
                "email": acc.email,
                "valid": False,
                "status": "error",
                "message": "未配置可用代理，无法执行 ChatGPT 发消息测试",
            }

        chatgpt_account = _build_chatgpt_message_account(acc, extra)
        result = send_test_message(chatgpt_account, proxy=proxy)
        _apply_chatgpt_message_result(
            acc,
            extra,
            updated_access_token=result.updated_access_token,
            updated_refresh_token=result.updated_refresh_token,
            invalid=result.invalid,
        )
        _report_chatgpt_proxy_result(result.used_proxy or proxy, ok=result.ok, invalid=result.invalid)

        return {
            "id": acc.id,
            "platform": acc.platform,
            "email": acc.email,
            "valid": result.ok,
            "status": "valid" if result.ok else ("invalid" if result.invalid else "error"),
            "message": result.message,
            "used_proxy": result.used_proxy or proxy,
        }

    PlatformCls = get(acc.platform)
    plugin = PlatformCls(config=config or RegisterConfig(extra=config_store.get_all()))
    valid = bool(plugin.check_valid(_build_runtime_account(acc)))
    return {
        "id": acc.id,
        "platform": acc.platform,
        "email": acc.email,
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "message": "账号有效" if valid else "检测未通过",
    }


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
):
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 500))
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))

    total = session.exec(
        select(func.count()).select_from(q.subquery())
    ).one()
    items = session.exec(
        q.order_by(AccountModel.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)
        
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/batch-check")
def batch_check_accounts(
    body: BatchCheckRequest,
    session: Session = Depends(get_session),
):
    account_ids = _normalize_account_ids(body.ids)
    load_all()

    rows = session.exec(
        select(AccountModel).where(AccountModel.id.in_(account_ids))
    ).all()
    row_map = {int(row.id): row for row in rows if row.id is not None}

    shared_config = RegisterConfig(extra=config_store.get_all())
    results: list[dict] = []
    invalid_ids: list[int] = []
    error_ids: list[int] = []
    not_found: list[int] = []
    dirty_rows: list[AccountModel] = []

    for account_id in account_ids:
        acc = row_map.get(account_id)
        if not acc:
            not_found.append(account_id)
            continue

        try:
            result = _check_account_validity(acc, config=shared_config)
            results.append(result)

            acc.updated_at = datetime.now(timezone.utc)
            if not result["valid"]:
                acc.status = AccountStatus.INVALID.value
                invalid_ids.append(account_id)
            dirty_rows.append(acc)
        except Exception as e:
            logger.exception("检测账号 %s 时出错", account_id)
            error_ids.append(account_id)
            results.append({
                "id": acc.id,
                "platform": acc.platform,
                "email": acc.email,
                "valid": False,
                "status": "error",
                "message": str(e) or "检测异常",
            })

    if dirty_rows:
        for row in dirty_rows:
            session.add(row)
        session.commit()

    return {
        "total_requested": len(account_ids),
        "tested": len(results),
        "valid": sum(1 for item in results if item["status"] == "valid"),
        "invalid": len(invalid_ids),
        "error": len(error_ids),
        "invalid_ids": invalid_ids,
        "error_ids": error_ids,
        "not_found": not_found,
        "items": results,
    }


@router.post("/{account_id}/chatgpt/chat-stream")
def chatgpt_chat_stream(
    account_id: int,
    body: ChatGPTConversationRequest,
    session: Session = Depends(get_session),
):
    acc = _get_chatgpt_account_or_404(account_id, session)
    extra = _parse_account_extra(acc.extra_json)
    prompt = str(body.prompt or "").strip()
    mode = str(body.mode or "official").strip().lower() or "official"
    stream = bool(body.stream)
    if not prompt:
        raise HTTPException(400, "消息不能为空")

    proxy = _resolve_chatgpt_proxy(acc, extra, preferred_proxy=str(body.proxy or "").strip())
    model = str(body.model or "").strip()
    if mode == "custom_api":
        return _stream_openai_compatible_chat(
            target_url=str(body.target_url or "").strip(),
            api_key=str(body.api_key or "").strip(),
            model=model or "gpt-4o-mini",
            messages=_normalize_chat_messages(body.messages, prompt),
            proxy=proxy,
            stream=stream,
        )

    chatgpt_account = _build_chatgpt_message_account(acc, extra)
    conversation_id = str(body.conversation_id or "").strip()
    parent_message_id = str(body.parent_message_id or "").strip()
    target_url = str(body.target_url or "").strip()
    proxy_for_log = proxy
    if "://" in proxy_for_log and "@" in proxy_for_log:
        scheme, remainder = proxy_for_log.split("://", 1)
        host = remainder.rsplit("@", 1)[-1]
        proxy_for_log = f"{scheme}://***@{host}"
    logger.info(
        "[chatgpt-chat-stream] account_id=%s mode=%s model=%s conversation_id=%s parent_message_id=%s target_url=%s proxy=%s stream=%s",
        account_id,
        mode,
        model or "auto",
        conversation_id,
        parent_message_id,
        target_url or "DEFAULT",
        proxy_for_log,
        stream,
    )

    def event_stream():
        proxy_reported = False
        try:
            for chunk in _iter_official_chat_chunks(
                chatgpt_account,
                proxy=proxy,
                prompt=prompt,
                model=model or "auto",
                conversation_id=conversation_id,
                parent_message_id=parent_message_id,
                target_url=target_url,
                stream=stream,
            ):
                event_name = str(chunk.get("event") or "message").strip() or "message"
                data = dict(chunk.get("data") or {})
                if event_name in {"error", "done"}:
                    logger.info(
                        "[chatgpt-chat-stream] account_id=%s event=%s chain=%s message=%s status=%s",
                        account_id,
                        event_name,
                        str(data.get("chain") or ""),
                        str(data.get("message") or ""),
                        str(data.get("response_status_code") or ""),
                    )
                if event_name == "meta":
                    data.setdefault("used_proxy", proxy)
                    data.setdefault("target_url", target_url or "https://chatgpt.com/backend-api/conversation")
                    data.setdefault("model", model or "auto")
                    data.setdefault("request_mode", "stream" if stream else "sync")
                    data["account_id"] = int(acc.id or account_id)
                    data["email"] = acc.email
                else:
                    data.setdefault("target_url", target_url or "https://chatgpt.com/backend-api/conversation")

                if event_name in {"done", "error"} and not proxy_reported:
                    used_proxy = str(data.get("used_proxy") or proxy).strip()
                    _report_chatgpt_proxy_result(
                        used_proxy,
                        ok=bool(data.get("ok")),
                        invalid=bool(data.get("invalid")),
                    )
                    _persist_chatgpt_message_result(
                        int(acc.id or account_id),
                        updated_access_token=str(data.get("updated_access_token") or "").strip(),
                        updated_refresh_token=str(data.get("updated_refresh_token") or "").strip(),
                        invalid=bool(data.get("invalid")),
                    )
                    proxy_reported = True

                yield _encode_sse(event_name, data)
        except Exception as e:
            logger.exception("[chatgpt-chat-stream] account_id=%s stream exception", account_id)
            if not proxy_reported:
                _report_chatgpt_proxy_result(proxy, ok=False, invalid=False)
            yield _encode_sse(
                "error",
                {
                    "ok": False,
                    "invalid": False,
                    "message": _exception_message(e, "对话发送失败"),
                    "used_proxy": proxy,
                },
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


@router.post("/{account_id}/chatgpt/models")
def chatgpt_models(
    account_id: int,
    body: ChatGPTModelsRequest,
    session: Session = Depends(get_session),
):
    acc = _get_chatgpt_account_or_404(account_id, session)
    extra = _parse_account_extra(acc.extra_json)
    proxy = _resolve_chatgpt_proxy(acc, extra, preferred_proxy=str(body.proxy or "").strip())
    chatgpt_account = _build_chatgpt_message_account(acc, extra)

    from platforms.chatgpt.message_tester import fetch_available_models

    result = fetch_available_models(
        chatgpt_account,
        proxy=proxy,
        target_url=str(body.target_url or "").strip(),
    )
    _report_chatgpt_proxy_result(result.used_proxy or proxy, ok=result.ok, invalid=result.invalid)
    _persist_chatgpt_message_result(
        int(acc.id or account_id),
        updated_access_token=result.updated_access_token,
        updated_refresh_token=result.updated_refresh_token,
        invalid=result.invalid,
    )

    if not result.ok:
        raise HTTPException(401 if result.invalid else 400, result.message)

    return {
        "ok": True,
        "message": result.message,
        "used_proxy": result.used_proxy or proxy,
        "models_url": result.models_url,
        "models": result.models,
        "data": result.data,
        "response_status_code": result.response_status_code,
    }


@router.post("/{account_id}/chatgpt/quota")
def chatgpt_quota(
    account_id: int,
    body: ChatGPTModelsRequest,
    session: Session = Depends(get_session),
):
    acc = _get_chatgpt_account_or_404(account_id, session)
    extra = _parse_account_extra(acc.extra_json)
    proxy = _resolve_chatgpt_proxy(acc, extra, preferred_proxy=str(body.proxy or "").strip())
    chatgpt_account = _build_chatgpt_message_account(acc, extra)

    from platforms.chatgpt.message_tester import fetch_official_quota

    result = fetch_official_quota(
        chatgpt_account,
        proxy=proxy,
        target_url=str(body.target_url or "").strip(),
    )
    _report_chatgpt_proxy_result(result.used_proxy or proxy, ok=result.ok, invalid=result.invalid)
    _persist_chatgpt_message_result(
        int(acc.id or account_id),
        updated_access_token=result.updated_access_token,
        updated_refresh_token=result.updated_refresh_token,
        invalid=result.invalid,
    )

    if not result.ok:
        raise HTTPException(401 if result.invalid else 400, result.message)

    return {
        "ok": True,
        "message": result.message,
        "used_proxy": result.used_proxy or proxy,
        "query_url": result.query_url,
        "summary": result.summary,
        "signals": result.signals,
        "data": result.data,
        "response_status_code": result.response_status_code,
    }


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return acc


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        try:
            result = _check_account_validity(acc)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    a.status = a.status if result["valid"] else AccountStatus.INVALID.value
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
