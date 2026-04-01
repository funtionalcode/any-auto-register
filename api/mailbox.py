from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter
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

router = APIRouter(prefix="/mailbox", tags=["mailbox"])


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
) -> dict[str, Any]:
    subject_text = str(subject or "").strip()
    sender_text = str(sender or "").strip()
    recipient_text = str(recipient or "").strip()
    content_text = str(content or "").strip()
    if content_text:
        try:
            content_text = mailbox._decode_raw_content(content_text) or content_text
        except Exception:
            pass
    preview_text = str(preview or "").strip() or _short_preview(content_text)
    verification_code = ""
    try:
        verification_code = str(
            mailbox._safe_extract(
                " ".join(
                    part
                    for part in [subject_text, preview_text, content_text]
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
                content=mail.get("content") or mail.get("html"),
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
                content=" ".join(
                    str(part or "")
                    for part in [mail.get("body"), mail.get("html")]
                    if str(part or "").strip()
                ),
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
                    for part in [
                        mail.get("content"),
                        mail.get("text"),
                        mail.get("html"),
                    ]
                    if str(part or "").strip()
                ),
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
                    content=" ".join(
                        str(part or "")
                        for part in [detail.get("text"), detail.get("html")]
                        if str(part or "").strip()
                    ),
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
                    content=" ".join(
                        str(part or "")
                        for part in [detail.get("text"), detail.get("html")]
                        if str(part or "").strip()
                    ),
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
                content=mail.get("raw"),
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
                        message.get("html"),
                    ]
                    if str(part or "").strip()
                ),
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
                content=" ".join(
                    str(part or "")
                    for part in [mail.body, mail.html_body]
                    if str(part or "").strip()
                ),
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
                    content=" ".join(
                        str(part or "")
                        for part in [detail_data.get("text"), detail_data.get("html")]
                        if str(part or "").strip()
                    ),
                )
            )
        return items

    raise RuntimeError(f"{provider} 暂未实现收件箱读取")


@router.post("/inbox/create")
def create_test_inbox(body: InboxCreateRequest):
    merged_config = _merge_mailbox_config(body.config)
    provider = _resolve_provider(body.provider, merged_config)
    mailbox = _build_mailbox(provider, merged_config, body.proxy)
    account = mailbox.get_email()
    return {
        "provider": provider,
        "email": account.email,
        "account_id": account.account_id,
        "extra": _extract_create_extra(provider, mailbox, account),
    }


@router.post("/inbox/messages")
def list_inbox_messages(body: InboxMessagesRequest):
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
