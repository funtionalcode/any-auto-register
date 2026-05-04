from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
import json
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.base_mailbox import (
    ApiMailMailbox,
    CFWorkerMailbox,
    DuckMailMailbox,
    FreemailMailbox,
    LaoudoMailbox,
    LuckMailMailbox,
    MailboxAccount,
    MaliAPIMailbox,
    MoeMailMailbox,
    SkyMailMailbox,
    TempMailLolMailbox,
    create_mailbox,
)
from core.config_store import config_store
from sqlmodel import Session, select as sql_select
from core.db import TempMailboxModel, engine as db_engine

router = APIRouter(prefix="/mailbox", tags=["mailbox"])

_mailbox_tasks: dict[str, dict[str, Any]] = {}
_mailbox_tasks_lock = threading.Lock()
_MAX_FINISHED_MAILBOX_TASKS = 50


class InboxCreateRequest(BaseModel):
    provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    proxy: str | None = None


class InboxMessagesRequest(BaseModel):
    provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    email: str | None = None
    account_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    proxy: str | None = None
    limit: int = 10


def _cleanup_mailbox_tasks() -> None:
    with _mailbox_tasks_lock:
        finished = [
            task_id
            for task_id, task in _mailbox_tasks.items()
            if task.get("status") in {"done", "failed"}
        ]
        if len(finished) <= _MAX_FINISHED_MAILBOX_TASKS:
            return
        finished.sort()
        for task_id in finished[: len(finished) - _MAX_FINISHED_MAILBOX_TASKS]:
            _mailbox_tasks.pop(task_id, None)


def _serialize_mailbox_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not task:
        return None
    return {
        "id": str(task.get("id") or ""),
        "action": str(task.get("action") or ""),
        "status": str(task.get("status") or "pending"),
        "message": str(task.get("message") or ""),
        "error_message": str(task.get("error_message") or ""),
        "result": dict(task.get("result") or {}),
        "created_at": float(task.get("created_at") or 0),
        "updated_at": float(task.get("updated_at") or 0),
        "finished_at": task.get("finished_at"),
    }


def _create_mailbox_task(action: str) -> dict[str, Any]:
    now = time.time()
    task_id = f"mailbox_{action}_{int(now * 1000)}"
    task = {
        "id": task_id,
        "action": action,
        "status": "pending",
        "message": "排队中",
        "error_message": "",
        "result": {},
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    with _mailbox_tasks_lock:
        _mailbox_tasks[task_id] = task
    _cleanup_mailbox_tasks()
    return _serialize_mailbox_task(task) or {}


def _get_mailbox_task(task_id: str) -> dict[str, Any] | None:
    with _mailbox_tasks_lock:
        return _serialize_mailbox_task(_mailbox_tasks.get(task_id))


def _update_mailbox_task(task_id: str, **changes: Any) -> None:
    with _mailbox_tasks_lock:
        task = _mailbox_tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = time.time()


def _merge_mailbox_config(raw_config: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(config_store.get_all())
    for key, value in (raw_config or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        merged[key] = value
    return merged


def _resolve_provider(provider: str | None, merged_config: dict[str, Any]) -> str:
    return str(provider or merged_config.get("mail_provider") or "moemail").strip()


def _short_preview(text: str, length: int = 160) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= length:
        return clean
    return f"{clean[: length - 3]}..."


def _format_created_at(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000.0
        try:
            return (
                datetime.fromtimestamp(timestamp, tz=timezone.utc)
                .astimezone()
                .isoformat()
            )
        except Exception:
            return str(value)
    text = str(value).strip()
    if not text:
        return ""
    return text


def _normalize_message(
    mailbox,
    *,
    message_id: Any,
    subject: Any = "",
    sender: Any = "",
    recipient: Any = "",
    created_at: Any = "",
    content: Any = "",
    preview: Any = "",
    html_content: Any = "",
    raw_content: Any = "",
) -> dict[str, Any]:
    subject_text = str(subject or "").strip()
    sender_text = str(sender or "").strip()
    recipient_text = str(recipient or "").strip()
    preview_text = str(preview or "").strip()
    if preview_text:
        try:
            preview_text = mailbox._decode_raw_content(preview_text) or preview_text
        except Exception:
            pass

    content_source = next(
        (
            value
            for value in [content, raw_content, html_content]
            if str(value or "").strip()
        ),
        "",
    )
    content_text = str(content_source or "").strip()
    if content_text:
        try:
            content_text = mailbox._decode_raw_content(content_text) or content_text
        except Exception:
            pass
    html_text = ""
    for candidate in [html_content, raw_content, content]:
        candidate_text = str(candidate or "").strip()
        if not candidate_text:
            continue
        try:
            html_text = mailbox._extract_html_content(candidate_text)
        except Exception:
            html_text = ""
        if html_text:
            break
    preview_text = preview_text or _short_preview(content_text)
    verification_code = ""
    try:
        html_plain = ""
        if html_text:
            try:
                html_plain = mailbox._decode_raw_content(html_text) or ""
            except Exception:
                html_plain = ""
        verification_code = str(
            mailbox._safe_extract(
                " ".join(
                    part
                    for part in [subject_text, preview_text, content_text, html_plain]
                    if str(part or "").strip()
                )
            )
            or ""
        ).strip()
    except Exception:
        verification_code = ""

    return {
        "id": str(message_id or "").strip(),
        "subject": subject_text,
        "from": sender_text,
        "to": recipient_text,
        "created_at": _format_created_at(created_at),
        "preview": preview_text,
        "content": content_text,
        "html": html_text,
        "verification_code": verification_code,
    }


def _build_mailbox(provider: str, merged_config: dict[str, Any], proxy: str | None):
    return create_mailbox(provider=provider, extra=merged_config, proxy=proxy)


def _extract_create_extra(provider: str, mailbox, account: MailboxAccount) -> dict[str, Any]:
    extra = dict(account.extra or {})
    if provider == "moemail":
        session = getattr(mailbox, "_session", None)
        if session is not None:
            for cookie in session.cookies:
                if "session-token" not in str(cookie.name or ""):
                    continue
                extra.setdefault("session_cookie_name", str(cookie.name))
                extra.setdefault("session_cookie_value", str(cookie.value))
                break
    return extra


def _resolve_account(mailbox, body: InboxMessagesRequest) -> MailboxAccount:
    email = str(body.email or "").strip()
    account_id = str(body.account_id or "").strip()
    extra = dict(body.extra or {})
    if email or account_id:
        return MailboxAccount(email=email, account_id=account_id, extra=extra)
    return mailbox.get_email()


def _list_messages(provider: str, mailbox, account: MailboxAccount, limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 10), 50))

    if isinstance(mailbox, LaoudoMailbox):
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            f"{mailbox.api}/list",
            params={
                "accountId": account.account_id,
                "allReceive": 0,
                "emailId": 0,
                "timeSort": 1,
                "size": limit,
                "type": 0,
            },
            headers={"authorization": mailbox.auth, "user-agent": mailbox._ua},
            timeout=15,
            impersonate="chrome131",
        )
        mails = response.json().get("data", {}).get("list", []) or []
        return [
            _normalize_message(
                mailbox,
                message_id=mail.get("id") or mail.get("emailId"),
                subject=mail.get("subject"),
                sender=mail.get("from"),
                recipient=account.email,
                created_at=mail.get("createTime") or mail.get("receiveTime"),
                content=mail.get("content"),
                html_content=mail.get("html"),
            )
            for mail in mails[:limit]
        ]

    if isinstance(mailbox, TempMailLolMailbox):
        import requests

        response = requests.get(
            f"{mailbox.api}/inbox",
            params={"token": account.account_id},
            proxies=mailbox.proxy,
            timeout=10,
        )
        mails = response.json().get("emails", []) or []
        return [
            _normalize_message(
                mailbox,
                message_id=mail.get("id"),
                subject=mail.get("subject"),
                sender=mail.get("from"),
                recipient=account.email,
                created_at=mail.get("date"),
                preview=mail.get("body"),
                content=mail.get("body"),
                html_content=mail.get("html"),
            )
            for mail in mails[:limit]
        ]

    if isinstance(mailbox, SkyMailMailbox):
        mails = mailbox._list_mails(account.account_id or account.email)
        return [
            _normalize_message(
                mailbox,
                message_id=mail.get("id")
                or mail.get("mailId")
                or mail.get("messageId")
                or idx + 1,
                subject=mail.get("subject"),
                sender=mail.get("from"),
                recipient=account.email,
                created_at=mail.get("date") or mail.get("time"),
                preview=mail.get("content"),
                content=" ".join(
                    str(part or "")
                    for part in [mail.get("content"), mail.get("text")]
                    if str(part or "").strip()
                ),
                html_content=mail.get("html"),
            )
            for idx, mail in enumerate(mails[:limit])
        ]

    if isinstance(mailbox, DuckMailMailbox):
        response = mailbox._request("GET", "/messages?page=1", token=account.account_id)
        messages = response.json().get("hydra:member", []) or []
        items: list[dict[str, Any]] = []
        for msg in messages[:limit]:
            message_id = str(msg.get("id") or msg.get("msgid") or "").strip()
            detail = {}
            if message_id:
                try:
                    detail = mailbox._request(
                        "GET",
                        f"/messages/{message_id}",
                        token=account.account_id,
                    ).json()
                except Exception:
                    detail = {}
            sender = ""
            if isinstance(msg.get("from"), dict):
                sender = str(msg.get("from", {}).get("address") or "").strip()
            items.append(
                _normalize_message(
                    mailbox,
                    message_id=message_id,
                    subject=detail.get("subject") or msg.get("subject"),
                    sender=sender,
                    recipient=account.email,
                    created_at=detail.get("createdAt") or msg.get("createdAt"),
                    preview=msg.get("intro"),
                    content=detail.get("text"),
                    html_content=detail.get("html"),
                )
            )
        return items

    if isinstance(mailbox, MaliAPIMailbox):
        messages = mailbox._list_messages(account)
        items: list[dict[str, Any]] = []
        for message in messages[:limit]:
            message_id = str(message.get("id") or "").strip()
            detail = mailbox._get_message_detail(message_id) if message_id else {}
            sender = ""
            if isinstance(detail.get("from"), dict):
                sender = str(detail.get("from", {}).get("address") or "").strip()
            items.append(
                _normalize_message(
                    mailbox,
                    message_id=message_id,
                    subject=detail.get("subject") or message.get("subject"),
                    sender=sender,
                    recipient=account.email,
                    created_at=detail.get("createdAt") or message.get("createdAt"),
                    preview=message.get("snippet"),
                    content=detail.get("text"),
                    html_content=detail.get("html"),
                )
            )
        return items

    if isinstance(mailbox, CFWorkerMailbox):
        mails = mailbox._get_mails(account.email)
        return [
            _normalize_message(
                mailbox,
                message_id=mail.get("id"),
                subject=mail.get("subject"),
                sender=mail.get("from"),
                recipient=account.email,
                created_at=mail.get("created_at"),
                preview=mail.get("raw"),
                raw_content=mail.get("raw"),
            )
            for mail in mails[:limit]
        ]

    if isinstance(mailbox, MoeMailMailbox):
        import requests

        cookie_name = str((account.extra or {}).get("session_cookie_name") or "").strip()
        cookie_value = str((account.extra or {}).get("session_cookie_value") or "").strip()
        if not cookie_name or not cookie_value:
            raise RuntimeError("MoeMail 读取收件箱需要先生成测试邮箱，或提供 session cookie 信息")
        session = requests.Session()
        session.proxies = mailbox.proxy
        hostname = urlparse(mailbox.api).hostname or ""
        if hostname:
            session.cookies.set(cookie_name, cookie_value, domain=hostname)
        else:
            session.cookies.set(cookie_name, cookie_value)
        response = session.get(f"{mailbox.api}/api/emails/{account.account_id}", timeout=10)
        messages = response.json().get("messages", []) or []
        return [
            _normalize_message(
                mailbox,
                message_id=message.get("id"),
                subject=message.get("subject"),
                sender=message.get("from"),
                recipient=account.email,
                created_at=message.get("createdAt"),
                preview=message.get("content") or message.get("text"),
                content=" ".join(
                    str(part or "")
                    for part in [
                        message.get("content"),
                        message.get("text"),
                        message.get("body"),
                    ]
                    if str(part or "").strip()
                ),
                html_content=message.get("html"),
                raw_content=message.get("body"),
            )
            for message in messages[:limit]
        ]

    if isinstance(mailbox, LuckMailMailbox):
        if not mailbox._use_purchase_mode(account):
            raise RuntimeError("LuckMail 订单接码模式暂不支持收件箱列表，请改用已购邮箱 Token 测试")
        token = account.account_id or mailbox._resolve_token(account)
        if not token:
            raise RuntimeError("LuckMail 缺少 Token，无法读取收件箱")
        mail_list = mailbox._client.user.get_token_mails(token)
        return [
            _normalize_message(
                mailbox,
                message_id=mail.message_id,
                subject=mail.subject,
                sender=mail.from_addr,
                recipient=account.email or getattr(mail_list, "email_address", ""),
                created_at=mail.received_at,
                preview=mail.body,
                content=mail.body,
                html_content=mail.html_body,
            )
            for mail in (mail_list.mails or [])[:limit]
        ]

    if isinstance(mailbox, FreemailMailbox):
        if not getattr(mailbox, "_session", None):
            mailbox._get_session()
        response = mailbox._session.get(
            f"{mailbox.api}/api/emails",
            params={"mailbox": account.email, "limit": limit},
            timeout=10,
        )
        messages = response.json() or []
        return [
            _normalize_message(
                mailbox,
                message_id=message.get("id"),
                subject=message.get("subject"),
                sender=message.get("from"),
                recipient=account.email,
                created_at=message.get("created_at"),
                preview=message.get("preview"),
                content=" ".join(
                    str(part or "")
                    for part in [
                        message.get("verification_code"),
                        message.get("preview"),
                        message.get("subject"),
                    ]
                    if str(part or "").strip()
                ),
            )
            for message in messages[:limit]
        ]

    if isinstance(mailbox, ApiMailMailbox):
        response = mailbox._request("GET", "/messages", token=account.account_id)
        if response.get("status_code") != 200:
            raise RuntimeError("Mail.tm 收件箱读取失败")
        messages = mailbox._mail_tm_items(response.get("data", {}))
        items: list[dict[str, Any]] = []
        for message in messages[:limit]:
            message_id = str(message.get("id") or "").strip()
            detail_data = {}
            if message_id:
                detail_response = mailbox._request(
                    "GET",
                    f"/messages/{message_id}",
                    token=account.account_id,
                )
                detail_data = mailbox._mail_tm_object(detail_response.get("data", {}))
            sender = ""
            if isinstance(message.get("from"), dict):
                sender = str(message.get("from", {}).get("address") or "").strip()
            items.append(
                _normalize_message(
                    mailbox,
                    message_id=message_id,
                    subject=detail_data.get("subject") or message.get("subject"),
                    sender=sender,
                    recipient=account.email,
                    created_at=detail_data.get("createdAt") or message.get("createdAt"),
                    preview=message.get("intro"),
                    content=detail_data.get("text"),
                    html_content=detail_data.get("html"),
                )
            )
        return items

    raise RuntimeError(f"{provider} 暂未实现收件箱读取")


def _save_temp_mailbox(email: str, provider: str, account_id: str, extra: dict) -> None:
    """自动保存临时邮箱记录到数据库"""
    try:
        with Session(db_engine) as session:
            existing = session.exec(
                sql_select(TempMailboxModel).where(TempMailboxModel.email == email)
            ).first()
            if not existing:
                import json as _json
                m = TempMailboxModel(
                    email=email,
                    provider=provider,
                    account_id=account_id,
                    extra_json=_json.dumps(extra, ensure_ascii=False),
                )
                session.add(m)
                session.commit()
    except Exception:
        pass


def _create_test_inbox_payload(body: InboxCreateRequest) -> dict[str, Any]:
    merged_config = _merge_mailbox_config(body.config)
    provider = _resolve_provider(body.provider, merged_config)
    mailbox = _build_mailbox(provider, merged_config, body.proxy)
    account = mailbox.get_email()
    extra = _extract_create_extra(provider, mailbox, account)
    _save_temp_mailbox(account.email, provider, account.account_id, extra)
    return {
        "provider": provider,
        "email": account.email,
        "account_id": account.account_id,
        "extra": extra,
    }


def _list_inbox_messages_payload(body: InboxMessagesRequest) -> dict[str, Any]:
    merged_config = _merge_mailbox_config(body.config)
    provider = _resolve_provider(body.provider, merged_config)
    mailbox = _build_mailbox(provider, merged_config, body.proxy)
    account = _resolve_account(mailbox, body)
    messages = _list_messages(provider, mailbox, account, body.limit)
    return {
        "provider": provider,
        "email": account.email,
        "account_id": account.account_id,
        "total": len(messages),
        "items": messages,
    }


def _run_mailbox_task(task_id: str, *, action: str, payload: dict[str, Any]) -> None:
    try:
        _update_mailbox_task(
            task_id,
            status="running",
            message="正在生成测试邮箱" if action == "create" else "正在刷新收件箱",
        )
        if action == "create":
            result = _create_test_inbox_payload(InboxCreateRequest(**payload))
        elif action == "messages":
            result = _list_inbox_messages_payload(InboxMessagesRequest(**payload))
        else:
            raise RuntimeError("未知邮箱任务类型")
        _update_mailbox_task(
            task_id,
            status="done",
            message="执行完成",
            result=result,
            finished_at=time.time(),
        )
    except Exception as exc:
        _update_mailbox_task(
            task_id,
            status="failed",
            message=str(exc) or "邮箱测试失败",
            error_message=str(exc) or "邮箱测试失败",
            finished_at=time.time(),
        )


@router.post("/inbox/create")
def create_test_inbox(body: InboxCreateRequest):
    return _create_test_inbox_payload(body)


@router.post("/inbox/create/async")
def create_test_inbox_async(body: InboxCreateRequest):
    task = _create_mailbox_task("create")
    thread = threading.Thread(
        target=_run_mailbox_task,
        args=(str(task["id"]),),
        kwargs={"action": "create", "payload": body.model_dump()},
        daemon=True,
    )
    thread.start()
    return task


@router.post("/inbox/messages")
def list_inbox_messages(body: InboxMessagesRequest):
    return _list_inbox_messages_payload(body)


@router.post("/inbox/messages/async")
def list_inbox_messages_async(body: InboxMessagesRequest):
    task = _create_mailbox_task("messages")
    thread = threading.Thread(
        target=_run_mailbox_task,
        args=(str(task["id"]),),
        kwargs={"action": "messages", "payload": body.model_dump()},
        daemon=True,
    )
    thread.start()
    return task


@router.get("/inboxes")
def list_temp_mailboxes():
    """列出所有已创建的临时邮箱"""
    with Session(db_engine) as session:
        items = session.exec(sql_select(TempMailboxModel).order_by(TempMailboxModel.id.desc())).all()
        return items


@router.get("/stats")
def mailbox_stats():
    """返回临时邮箱统计信息"""
    with Session(db_engine) as session:
        total = len(session.exec(sql_select(TempMailboxModel)).all())
        # 按提供商分组统计
        rows = session.exec(sql_select(TempMailboxModel)).all()
        by_provider: dict[str, int] = {}
        for row in rows:
            provider = row.provider or "unknown"
            by_provider[provider] = by_provider.get(provider, 0) + 1
    return {"total": total, "by_provider": by_provider}


class SaveInboxRequest(BaseModel):
    email: str
    provider: str = ""
    account_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


@router.post("/inboxes")
def save_temp_mailbox(body: SaveInboxRequest):
    """保存已创建的临时邮箱记录"""
    with Session(db_engine) as session:
        existing = session.exec(
            sql_select(TempMailboxModel).where(TempMailboxModel.email == body.email)
        ).first()
        if existing:
            return existing
        m = TempMailboxModel(
            email=body.email,
            provider=body.provider,
            account_id=body.account_id,
            extra_json=str(__import__("json").dumps(body.extra, ensure_ascii=False)),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return m


class DeleteInboxRequest(BaseModel):
    ids: list[int]


@router.post("/inboxes/batch-delete")
def batch_delete_temp_mailboxes(body: DeleteInboxRequest):
    """批量删除临时邮箱记录"""
    if not body.ids:
        raise HTTPException(400, "ID 列表不能为空")
    with Session(db_engine) as session:
        items = session.exec(
            sql_select(TempMailboxModel).where(TempMailboxModel.id.in_(body.ids))
        ).all()
        for item in items:
            session.delete(item)
        session.commit()
    return {"deleted": len(items)}


@router.post("/inboxes/{mailbox_id}/messages")
def get_temp_mailbox_messages(mailbox_id: int, body: InboxMessagesRequest | None = None):
    """查看指定临时邮箱的邮件"""
    with Session(db_engine) as session:
        m = session.get(TempMailboxModel, mailbox_id)
        if not m:
            raise HTTPException(404, "临时邮箱记录不存在")
    req = body or InboxMessagesRequest()
    req.email = m.email
    req.account_id = m.account_id
    if not req.provider and m.provider:
        req.provider = m.provider
    extra = m.get_extra()
    if not req.config and extra:
        req.config = extra
    return _list_inbox_messages_payload(req)


@router.get("/inbox/tasks/{task_id}")
def get_mailbox_task_snapshot(task_id: str):
    snapshot = _get_mailbox_task(task_id)
    if not snapshot:
        raise HTTPException(404, "邮箱测试任务不存在")
    return snapshot
