#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session

from core.config_store import config_store
from core.db import AccountModel, engine
from core.proxy_pool import proxy_pool
from platforms.chatgpt.message_tester import TEST_PROMPT, send_test_message


DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test a ChatGPT account by sending a real message through a proxy.")
    parser.add_argument("--account-id", type=int, help="Saved ChatGPT account ID in local database")
    parser.add_argument("--proxy", default="", help="Proxy URL. If omitted, tries account extra.test_proxy, chatgpt_test_proxy, then proxy pool")
    parser.add_argument("--prompt", default=TEST_PROMPT, help="Test prompt to send")
    parser.add_argument("--access-token", default="", help="Manual access token")
    parser.add_argument("--refresh-token", default="", help="Manual refresh token")
    parser.add_argument("--session-token", default="", help="Manual __Secure-next-auth.session-token")
    parser.add_argument("--cookies", default="", help="Manual cookie string")
    parser.add_argument("--email", default="manual@example.com", help="Email label used in output when testing manually")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="OAuth client_id for refresh token flow")
    parser.add_argument("--write-back", action="store_true", help="Persist refreshed tokens / invalid status back to DB when using --account-id")
    return parser.parse_args()


def parse_extra(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_saved_account(account_id: int) -> tuple[AccountModel, dict]:
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if not row:
            raise SystemExit(f"Account #{account_id} not found")
        if row.platform != "chatgpt":
            raise SystemExit(f"Account #{account_id} is {row.platform}, not chatgpt")
        extra = parse_extra(row.extra_json)
        return row, extra


def resolve_proxy(cli_proxy: str, extra: dict, region: str = "") -> str:
    proxy = str(cli_proxy or "").strip()
    if proxy:
        return proxy

    proxy = str(extra.get("test_proxy") or config_store.get("chatgpt_test_proxy", "") or "").strip()
    if proxy:
        return proxy

    proxy = proxy_pool.get_next(region=region or "") or proxy_pool.get_next() or ""
    return str(proxy or "").strip()


def build_manual_account(args: argparse.Namespace) -> tuple[SimpleNamespace, dict, AccountModel | None]:
    extra = {
        "access_token": str(args.access_token or "").strip(),
        "refresh_token": str(args.refresh_token or "").strip(),
        "session_token": str(args.session_token or "").strip(),
        "cookies": str(args.cookies or "").strip(),
    }

    account = SimpleNamespace(
        email=str(args.email or "manual@example.com").strip() or "manual@example.com",
        access_token=extra["access_token"],
        refresh_token=extra["refresh_token"],
        session_token=extra["session_token"],
        cookies=extra["cookies"],
        id_token="",
        client_id=str(args.client_id or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
    )
    return account, extra, None


def build_saved_account(args: argparse.Namespace) -> tuple[SimpleNamespace, dict, AccountModel]:
    row, extra = load_saved_account(args.account_id)
    account = SimpleNamespace(
        email=row.email,
        access_token=str(extra.get("access_token") or row.token or "").strip(),
        refresh_token=str(extra.get("refresh_token") or "").strip(),
        session_token=str(extra.get("session_token") or "").strip(),
        cookies=str(extra.get("cookies") or "").strip(),
        id_token=str(extra.get("id_token") or "").strip(),
        client_id=str(extra.get("client_id") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
    )
    return account, extra, row


def maybe_write_back(row: AccountModel | None, extra: dict, result, *, enabled: bool) -> None:
    if not enabled or row is None:
        return

    changed = False
    if result.updated_access_token:
        row.token = result.updated_access_token
        extra["access_token"] = result.updated_access_token
        changed = True
    if result.updated_refresh_token:
        extra["refresh_token"] = result.updated_refresh_token
        changed = True
    if result.invalid and row.status != "invalid":
        row.status = "invalid"
        changed = True
    if not changed:
        return

    row.extra_json = json.dumps(extra, ensure_ascii=False)

    with Session(engine) as session:
        current = session.get(AccountModel, row.id)
        if not current:
            return
        current.token = row.token
        current.status = row.status
        current.extra_json = row.extra_json
        session.add(current)
        session.commit()


def main() -> int:
    args = parse_args()

    if args.account_id:
        account, extra, row = build_saved_account(args)
        region = row.region or ""
    else:
        if not any([args.access_token, args.refresh_token, args.session_token]):
            raise SystemExit("Provide --account-id or at least one of --access-token / --refresh-token / --session-token")
        account, extra, row = build_manual_account(args)
        region = ""

    proxy = resolve_proxy(args.proxy, extra, region=region)
    if not proxy:
        payload = {
            "ok": False,
            "invalid": False,
            "message": "No proxy available. Pass --proxy or configure proxy pool / chatgpt_test_proxy.",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    result = send_test_message(account, proxy=proxy, prompt=args.prompt)
    maybe_write_back(row, extra, result, enabled=bool(args.write_back))

    payload = {
        "ok": result.ok,
        "invalid": result.invalid,
        "message": result.message,
        "response_excerpt": result.response_excerpt,
        "conversation_id": result.conversation_id,
        "used_proxy": result.used_proxy or proxy,
        "updated_access_token": bool(result.updated_access_token),
        "updated_refresh_token": bool(result.updated_refresh_token),
        "write_back": bool(args.write_back and row is not None),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if result.ok:
        return 0
    if result.invalid:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
