import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

from core.base_platform import Account, AccountStatus, RegisterConfig
from core.config_store import config_store
from core.db import AccountModel, get_session
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


def _check_account_validity(acc: AccountModel, *, config: RegisterConfig | None = None) -> dict:
    if acc.platform == "chatgpt":
        from core.proxy_pool import proxy_pool
        from platforms.chatgpt.message_tester import send_test_message

        extra = _parse_account_extra(acc.extra_json)
        preferred_proxy = str(
            extra.get("test_proxy")
            or config_store.get("chatgpt_test_proxy", "")
            or ""
        ).strip()
        proxy = preferred_proxy or proxy_pool.get_next(region=acc.region or "") or proxy_pool.get_next()
        if not proxy:
            return {
                "id": acc.id,
                "platform": acc.platform,
                "email": acc.email,
                "valid": False,
                "status": "error",
                "message": "未配置可用代理，无法执行 ChatGPT 发消息测试",
            }

        account = _build_runtime_account(acc)

        class _ChatGPTAccount:
            pass

        chatgpt_account = _ChatGPTAccount()
        chatgpt_account.email = account.email
        chatgpt_account.access_token = extra.get("access_token") or acc.token
        chatgpt_account.refresh_token = extra.get("refresh_token", "")
        chatgpt_account.id_token = extra.get("id_token", "")
        chatgpt_account.session_token = extra.get("session_token", "")
        chatgpt_account.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        chatgpt_account.cookies = extra.get("cookies", "")

        result = send_test_message(chatgpt_account, proxy=proxy)

        if result.updated_access_token:
            extra["access_token"] = result.updated_access_token
            acc.token = result.updated_access_token
        if result.updated_refresh_token:
            extra["refresh_token"] = result.updated_refresh_token
        if result.updated_access_token or result.updated_refresh_token:
            acc.extra_json = json.dumps(extra, ensure_ascii=False)

        if result.ok or result.invalid:
            proxy_pool.report_success(proxy)
        else:
            proxy_pool.report_fail(proxy)

        return {
            "id": acc.id,
            "platform": acc.platform,
            "email": acc.email,
            "valid": result.ok,
            "status": "valid" if result.ok else ("invalid" if result.invalid else "error"),
            "message": result.message,
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
