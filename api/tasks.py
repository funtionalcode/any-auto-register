import asyncio
import json
import logging
import threading
import time
from copy import deepcopy
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, func, select

from core.db import TaskLog, engine

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

_tasks: dict = {}
_tasks_lock = threading.Lock()

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250


def _cleanup_old_tasks():
    """Remove oldest finished tasks when the dict grows too large."""
    with _tasks_lock:
        finished = [
            (tid, t)
            for tid, t in _tasks.items()
            if t.get("status") in ("done", "failed")
        ]
        if len(finished) <= MAX_FINISHED_TASKS:
            return
        finished.sort(key=lambda x: x[0])
        to_remove = finished[: len(finished) - MAX_FINISHED_TASKS]
        for tid, _ in to_remove:
            del _tasks[tid]


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store

    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)

    mail_provider = prepared.extra.get("mail_provider") or config_store.get(
        "mail_provider", ""
    )
    if mail_provider == "luckmail":
        platform = prepared.platform
        if platform in ("tavily", "openblocklabs"):
            raise HTTPException(400, f"LuckMail 渠道暂时不支持 {platform} 项目注册")

        mapping = {
            "trae": "trae",
            "cursor": "cursor",
            "grok": "grok",
            "kiro": "kiro",
            "chatgpt": "openai",
        }
        prepared.extra["luckmail_project_code"] = mapping.get(platform, platform)

    return prepared


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    now = time.time()
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "pending",
            "platform": req.platform,
            "source": source,
            "meta": meta or {},
            "progress": f"0/{req.count}",
            "logs": [],
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "log_count": 0,
            "latest_log": "",
        }


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    prepared = _prepare_register_request(req)
    task_id = f"task_{int(time.time() * 1000)}"
    _create_task_record(task_id, prepared, source, meta)
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_register, args=(task_id, prepared), daemon=True
        )
        thread.start()
    else:
        background_tasks.add_task(_run_register, task_id, prepared)
    return task_id


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    with _tasks_lock:
        for task in _tasks.values():
            if task.get("status") not in ("pending", "running"):
                continue
            if platform and task.get("platform") != platform:
                continue
            if source and task.get("source") != source:
                continue
            return True
    return False


def _log(task_id: str, msg: str):
    """向任务追加一条日志"""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _tasks_lock:
        if task_id in _tasks:
            task = _tasks[task_id]
            task.setdefault("logs", []).append(entry)
            task["updated_at"] = time.time()
            task["log_count"] = len(task.get("logs", []))
            task["latest_log"] = entry
    logger.info("[Task %s] %s", task_id, msg)


def _parse_task_log_detail(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _build_task_log_summary(detail: dict[str, Any] | None) -> dict[str, Any]:
    payload = detail or {}
    logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
    attempt_no = _normalize_positive_int(payload.get("attempt_no"))
    total_count = _normalize_positive_int(payload.get("total_count"))
    log_count = _normalize_positive_int(payload.get("log_count")) or len(logs)
    duration_ms = _normalize_positive_int(payload.get("duration_ms")) or 0

    return {
        "task_id": str(payload.get("task_id") or "").strip(),
        "attempt_no": attempt_no,
        "total_count": total_count,
        "source": str(payload.get("source") or "").strip(),
        "proxy": str(payload.get("proxy") or "").strip(),
        "has_logs": bool(logs),
        "log_count": log_count,
        "latest_log": str(logs[-1] or "").strip() if logs else "",
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "duration_ms": duration_ms,
    }


def _serialize_task_log(item: TaskLog, *, include_detail: bool = False) -> dict[str, Any]:
    detail = _parse_task_log_detail(item.detail_json)
    payload = {
        "id": item.id,
        "platform": item.platform,
        "email": item.email,
        "status": item.status,
        "error": item.error,
        "detail_json": item.detail_json,
        "detail_summary": _build_task_log_summary(detail),
        "created_at": item.created_at,
    }
    if include_detail:
        payload["detail"] = detail
    return payload


def _build_task_log_detail(
    *,
    task_id: str,
    attempt_no: int,
    total_count: int,
    logs: list[str] | None = None,
    source: str = "manual",
    meta: dict | None = None,
    proxy: str | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    request: dict[str, Any] | None = None,
) -> dict:
    normalized_logs = list(logs or [])
    detail: dict[str, object] = {
        "task_id": task_id,
        "attempt_no": attempt_no,
        "total_count": total_count,
        "source": source,
        "logs": normalized_logs,
        "log_count": len(normalized_logs),
    }
    if meta:
        detail["meta"] = meta
    if proxy:
        detail["proxy"] = proxy
    if request:
        detail["request"] = request
    if started_at is not None:
        detail["started_at"] = float(started_at)
    if finished_at is not None:
        detail["finished_at"] = float(finished_at)
    if started_at is not None and finished_at is not None:
        detail["duration_ms"] = max(0, int((finished_at - started_at) * 1000))
    return detail


def _save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict = None
):
    """Write a TaskLog record to the database."""
    with Session(engine) as s:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )
        s.add(log)
        s.commit()


def _auto_upload_integrations(task_id: str, account, log_fn=None):
    """注册成功后自动导入外部系统。"""
    emit = log_fn or (lambda msg: _log(task_id, msg))
    try:
        from services.external_sync import sync_account

        for result in sync_account(account):
            name = result.get("name", "Auto Upload")
            ok = bool(result.get("ok"))
            msg = result.get("msg", "")
            emit(f"  [{name}] {'✓ ' + msg if ok else '✗ ' + msg}")
    except Exception as e:
        emit(f"  [Auto Upload] 自动导入异常: {e}")


def _run_register(task_id: str, req: RegisterTaskRequest):
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.base_mailbox import create_mailbox
    from core.proxy_utils import normalize_proxy_url

    with _tasks_lock:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["updated_at"] = time.time()
        task_source = str(_tasks[task_id].get("source") or "manual")
        task_meta = deepcopy(_tasks[task_id].get("meta") or {})
    success = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()
    task_request_summary = {
        "platform": req.platform,
        "count": req.count,
        "concurrency": req.concurrency,
        "register_delay_seconds": req.register_delay_seconds,
        "executor_type": req.executor_type,
        "captcha_solver": req.captcha_solver,
        "has_proxy": bool(str(req.proxy or "").strip()),
    }

    try:
        PlatformCls = get(req.platform)

        def _build_mailbox(proxy: Optional[str]):
            from core.config_store import config_store

            merged_extra = config_store.get_all().copy()
            merged_extra.update(
                {k: v for k, v in req.extra.items() if v is not None and v != ""}
            )
            return create_mailbox(
                provider=merged_extra.get("mail_provider", "laoudo"),
                extra=merged_extra,
                proxy=proxy,
            )

        def _do_one(i: int):
            nonlocal next_start_time
            attempt_logs: list[str] = []
            _proxy = None
            attempt_started_at = time.time()

            def _attempt_log(msg: str):
                ts = time.strftime("%H:%M:%S")
                attempt_logs.append(f"[{ts}] {msg}")
                _log(task_id, f"[{i + 1}/{req.count}] {msg}")

            try:
                from core.proxy_pool import proxy_pool

                _proxy = req.proxy
                if not _proxy:
                    _proxy = proxy_pool.get_next()
                _proxy = normalize_proxy_url(_proxy)
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _attempt_log(
                                f"启动前延迟 {wait_seconds:g} 秒"
                            )
                            time.sleep(wait_seconds)
                        next_start_time = time.time() + req.register_delay_seconds
                from core.config_store import config_store

                merged_extra = config_store.get_all().copy()
                merged_extra.update(
                    {k: v for k, v in req.extra.items() if v is not None and v != ""}
                )

                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                _mailbox = _build_mailbox(_proxy)
                _platform = PlatformCls(config=_config, mailbox=_mailbox)
                _platform._log_fn = _attempt_log
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._log_fn = _attempt_log
                with _tasks_lock:
                    _tasks[task_id]["progress"] = f"{i + 1}/{req.count}"
                _attempt_log("开始注册")
                if _proxy:
                    _attempt_log(f"使用代理: {_proxy}")
                account = _platform.register(
                    email=req.email or None,
                    password=req.password,
                )
                if isinstance(account.extra, dict):
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault(
                                "luckmail_project_code",
                                merged_extra.get("luckmail_project_code"),
                            )
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault(
                                "luckmail_email_type",
                                merged_extra.get("luckmail_email_type"),
                            )
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault(
                                "luckmail_domain", merged_extra.get("luckmail_domain")
                            )
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault(
                                "luckmail_base_url",
                                merged_extra.get("luckmail_base_url"),
                            )
                saved_account = save_account(account)
                if _proxy:
                    proxy_pool.report_success(_proxy)
                _attempt_log(f"✓ 注册成功: {account.email}")
                _auto_upload_integrations(
                    task_id,
                    saved_account or account,
                    log_fn=_attempt_log,
                )
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _attempt_log(f"  [升级链接] {cashier_url}")
                    with _tasks_lock:
                        _tasks[task_id].setdefault("cashier_urls", []).append(
                            cashier_url
                        )
                _save_task_log(
                    req.platform,
                    account.email,
                    "success",
                    detail=_build_task_log_detail(
                        task_id=task_id,
                        attempt_no=i + 1,
                        total_count=req.count,
                        logs=attempt_logs,
                        source=task_source,
                        meta=task_meta,
                        proxy=_proxy,
                        started_at=attempt_started_at,
                        finished_at=time.time(),
                        request=task_request_summary,
                    ),
                )
                return True
            except Exception as e:
                if _proxy:
                    proxy_pool.report_fail(_proxy)
                _attempt_log(f"✗ 注册失败: {e}")
                _save_task_log(
                    req.platform,
                    req.email or "",
                    "failed",
                    error=str(e),
                    detail=_build_task_log_detail(
                        task_id=task_id,
                        attempt_no=i + 1,
                        total_count=req.count,
                        logs=attempt_logs,
                        source=task_source,
                        meta=task_meta,
                        proxy=_proxy,
                        started_at=attempt_started_at,
                        finished_at=time.time(),
                        request=task_request_summary,
                    ),
                )
                return str(e)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(req.concurrency, req.count, 5)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, i) for i in range(req.count)]
            for f in as_completed(futures):
                try:
                    result = f.result()
                except Exception as e:
                    _log(task_id, f"✗ 任务线程异常: {e}")
                    errors.append(str(e))
                    continue
                if result is True:
                    success += 1
                else:
                    errors.append(result)
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        with _tasks_lock:
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["error"] = str(e)
            _tasks[task_id]["updated_at"] = time.time()
            _tasks[task_id]["finished_at"] = time.time()
        return

    with _tasks_lock:
        _tasks[task_id]["status"] = "done"
        _tasks[task_id]["success"] = success
        _tasks[task_id]["errors"] = errors
        _tasks[task_id]["updated_at"] = time.time()
        _tasks[task_id]["finished_at"] = time.time()
    _log(task_id, f"完成: 成功 {success} 个, 失败 {len(errors)} 个")
    _cleanup_old_tasks()


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.get("/logs")
def get_logs(
    platform: str = "",
    status: str = "",
    source: str = "",
    email: str = "",
    keyword: str = "",
    task_id: str = "",
    page: int = 1,
    page_size: int = 50,
    include_detail: bool = False,
):
    current_page = max(1, int(page or 1))
    size = max(1, min(int(page_size or 50), 100))
    conditions = []

    platform = str(platform or "").strip()
    status = str(status or "").strip()
    source = str(source or "").strip()
    email = str(email or "").strip()
    keyword = str(keyword or "").strip()
    task_id = str(task_id or "").strip()

    if platform:
        conditions.append(TaskLog.platform == platform)
    if status:
        conditions.append(TaskLog.status == status)
    if email:
        conditions.append(TaskLog.email.contains(email))
    if source:
        conditions.append(TaskLog.detail_json.contains(f'"source": "{source}"'))
    if task_id:
        conditions.append(TaskLog.detail_json.contains(f'"task_id": "{task_id}"'))
    if keyword:
        conditions.append(
            or_(
                TaskLog.platform.contains(keyword),
                TaskLog.email.contains(keyword),
                TaskLog.error.contains(keyword),
                TaskLog.detail_json.contains(keyword),
            )
        )

    with Session(engine) as s:
        list_statement = select(TaskLog)
        count_statement = select(func.count()).select_from(TaskLog)
        for condition in conditions:
            list_statement = list_statement.where(condition)
            count_statement = count_statement.where(condition)

        total = int(s.exec(count_statement).one() or 0)
        rows = s.exec(
            list_statement
            .order_by(TaskLog.id.desc())
            .offset((current_page - 1) * size)
            .limit(size)
        ).all()

    return {
        "page": current_page,
        "page_size": size,
        "total": total,
        "items": [
            _serialize_task_log(item, include_detail=include_detail)
            for item in rows
        ],
    }


@router.get("/logs/{log_id}")
def get_log_detail(log_id: int):
    with Session(engine) as s:
        item = s.get(TaskLog, log_id)
        if not item:
            raise HTTPException(404, "任务历史不存在")
        return _serialize_task_log(item, include_detail=True)


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")

    async def event_generator():
        sent = max(0, int(since or 0))
        initial_snapshot_sent = False
        while True:
            with _tasks_lock:
                task = deepcopy(_tasks.get(task_id, {}))
                logs = list(task.get("logs", []))
                status = str(task.get("status", "") or "")
            if not initial_snapshot_sent:
                payload = {
                    "snapshot": task,
                    "since": len(logs),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                initial_snapshot_sent = True
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent], 'index': sent}, ensure_ascii=False)}\n\n"
                sent += 1
            if status in ("done", "failed"):
                yield (
                    f"data: {json.dumps({'done': True, 'status': status, 'task': task}, ensure_ascii=False)}\n\n"
                )
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    with _tasks_lock:
        if task_id not in _tasks:
            raise HTTPException(404, "任务不存在")
        return _tasks[task_id]


@router.get("")
def list_tasks():
    with _tasks_lock:
        return list(_tasks.values())
