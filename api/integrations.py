from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.base_platform import Account, AccountStatus
from core.db import AccountModel, engine
from services.external_apps import install, list_status, read_log, start, start_all, stop, stop_all
from services.chatgpt_sync import backfill_chatgpt_account_to_cpa, get_cliproxy_sync_state

router = APIRouter(prefix="/integrations", tags=["integrations"])
logger = logging.getLogger(__name__)

_backfill_tasks: dict[str, dict[str, Any]] = {}
_backfill_tasks_lock = threading.Lock()
_MAX_FINISHED_BACKFILL_TASKS = 50


class BackfillRequest(BaseModel):
    platforms: list[str] = Field(default_factory=lambda: ["grok", "kiro"])
    account_ids: list[int] = Field(default_factory=list)
    pending_only: bool = False
    status: Optional[str] = None
    email: Optional[str] = None


def _to_account(model: AccountModel) -> Account:
    return Account(
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        region=model.region,
        token=model.token,
        status=AccountStatus(model.status),
        extra=model.get_extra(),
    )


def _cleanup_backfill_tasks() -> None:
    with _backfill_tasks_lock:
        finished = [
            task_id
            for task_id, task in _backfill_tasks.items()
            if task.get("status") in {"done", "failed"}
        ]
        if len(finished) <= _MAX_FINISHED_BACKFILL_TASKS:
            return
        finished.sort()
        for task_id in finished[: len(finished) - _MAX_FINISHED_BACKFILL_TASKS]:
            _backfill_tasks.pop(task_id, None)


def _serialize_backfill_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(task.get("id") or ""),
        "status": str(task.get("status") or "pending"),
        "message": str(task.get("message") or ""),
        "error": str(task.get("error") or ""),
        "total": int(task.get("total") or 0),
        "target_total": int(task.get("target_total") or 0),
        "success": int(task.get("success") or 0),
        "failed": int(task.get("failed") or 0),
        "skipped": int(task.get("skipped") or 0),
        "items": list(task.get("items") or []),
        "logs": list(task.get("logs") or []),
        "request": dict(task.get("request") or {}),
        "created_at": float(task.get("created_at") or 0),
        "updated_at": float(task.get("updated_at") or 0),
        "finished_at": task.get("finished_at"),
    }


def _create_backfill_task(body: BackfillRequest) -> dict[str, Any]:
    now = time.time()
    task_id = f"backfill_{int(now * 1000)}"
    task = {
        "id": task_id,
        "status": "pending",
        "message": "排队中",
        "error": "",
        "total": 0,
        "target_total": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "items": [],
        "logs": [],
        "request": body.model_dump(),
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    with _backfill_tasks_lock:
        _backfill_tasks[task_id] = task
    _cleanup_backfill_tasks()
    return _serialize_backfill_task(task)


def _get_backfill_task(task_id: str) -> dict[str, Any] | None:
    with _backfill_tasks_lock:
        task = _backfill_tasks.get(task_id)
        return _serialize_backfill_task(task) if task else None


def _get_active_backfill_task() -> dict[str, Any] | None:
    with _backfill_tasks_lock:
        for task in _backfill_tasks.values():
            if task.get("status") in {"pending", "running"}:
                return _serialize_backfill_task(task)
    return None


def _update_backfill_task(task_id: str, **changes: Any) -> None:
    with _backfill_tasks_lock:
        task = _backfill_tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = time.time()


def _append_backfill_task_log(task_id: str, message: str) -> None:
    with _backfill_tasks_lock:
        task = _backfill_tasks.get(task_id)
        if not task:
            return
        logs = task.setdefault("logs", [])
        logs.append(message)
        if len(logs) > 80:
            del logs[:-80]
        task["updated_at"] = time.time()


def _record_backfill_task_progress(task_id: str, summary: dict[str, Any], item: dict[str, Any], *, target_total: int) -> None:
    with _backfill_tasks_lock:
        task = _backfill_tasks.get(task_id)
        if not task:
            return
        items = task.setdefault("items", [])
        items.append(item)
        task["total"] = int(summary.get("total") or 0)
        task["target_total"] = int(target_total or 0)
        task["success"] = int(summary.get("success") or 0)
        task["failed"] = int(summary.get("failed") or 0)
        task["skipped"] = int(summary.get("skipped") or 0)
        task["message"] = f"已处理 {task['total']}/{task['target_total'] or task['total']}"
        task["updated_at"] = time.time()


def _run_backfill(body: BackfillRequest, *, task_id: str | None = None) -> dict[str, Any]:
    summary = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "items": []}
    targets = set(body.platforms or [])

    if task_id:
        _update_backfill_task(task_id, status="running", message="正在收集账号")

    with Session(engine) as s:
        q = select(AccountModel)
        if body.account_ids:
            q = q.where(AccountModel.id.in_(body.account_ids))
            if targets:
                q = q.where(AccountModel.platform.in_(targets))
        elif targets:
            q = q.where(AccountModel.platform.in_(targets))
        else:
            if task_id:
                _update_backfill_task(task_id, status="done", message="没有可处理的平台", finished_at=time.time())
            return summary

        if body.status:
            q = q.where(AccountModel.status == body.status)
        if body.email:
            q = q.where(AccountModel.email.contains(body.email))

        rows = s.exec(q).all()
        if body.pending_only:
            rows = [
                row for row in rows
                if row.platform != "chatgpt"
                or str(get_cliproxy_sync_state(row).get("remote_state") or "").strip().lower() == "not_found"
            ]

        if task_id:
            _update_backfill_task(task_id, target_total=len(rows), message=f"待处理 {len(rows)} 个账号")

        if any(row.platform == "grok" for row in rows):
            from services.grok2api_runtime import ensure_grok2api_ready

            ok, msg = ensure_grok2api_ready()
            if not ok:
                result = {
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "items": [{"platform": "grok", "email": "", "results": [{"name": "grok2api", "ok": False, "msg": msg}]}],
                }
                if task_id:
                    _update_backfill_task(
                        task_id,
                        status="failed",
                        error=msg,
                        message=msg,
                        items=result["items"],
                        finished_at=time.time(),
                    )
                return result

        for index, row in enumerate(rows, start=1):
            item = {"platform": row.platform, "email": row.email, "results": []}
            if task_id:
                _append_backfill_task_log(task_id, f"{index}/{len(rows)} {row.platform} {row.email}")
            try:
                results = []
                if row.platform == "chatgpt":
                    outcome = backfill_chatgpt_account_to_cpa(row, session=s, commit=True)
                    ok = bool(outcome.get("ok"))
                    skipped = bool(outcome.get("skipped"))
                    results.extend(outcome.get("results") or [])
                    if not results:
                        results.append({"name": "CLIProxyAPI", "ok": ok, "msg": outcome.get("message", "")})
                    if skipped:
                        summary["skipped"] += 1
                    elif ok:
                        summary["success"] += 1
                    else:
                        summary["failed"] += 1

                elif row.platform == "grok":
                    from core.config_store import config_store
                    from platforms.grok.grok2api_upload import upload_to_grok2api

                    account = _to_account(row)
                    api_url = str(config_store.get("grok2api_url", "") or "").strip() or "http://127.0.0.1:8011"
                    app_key = str(config_store.get("grok2api_app_key", "") or "").strip() or "grok2api"
                    ok, msg = upload_to_grok2api(account, api_url=api_url, app_key=app_key)
                    results.append({"name": "grok2api", "ok": ok, "msg": msg})

                elif row.platform == "kiro":
                    from core.config_store import config_store
                    from platforms.kiro.account_manager_upload import upload_to_kiro_manager

                    account = _to_account(row)
                    configured_path = str(config_store.get("kiro_manager_path", "") or "").strip() or None
                    ok, msg = upload_to_kiro_manager(account, path=configured_path)
                    results.append({"name": "Kiro Manager", "ok": ok, "msg": msg})

                if not results:
                    item["results"].append({"name": "skip", "ok": False, "msg": "未配置对应导入目标"})
                    summary["failed"] += 1
                else:
                    item["results"] = results
                    if row.platform != "chatgpt":
                        if all(r.get("ok") for r in results):
                            summary["success"] += 1
                        else:
                            summary["failed"] += 1
            except Exception as e:
                s.rollback()
                logger.exception("Integration backfill failed for %s/%s", row.platform, row.email)
                item["results"].append({"name": "error", "ok": False, "msg": str(e)})
                summary["failed"] += 1

            summary["items"].append(item)
            summary["total"] += 1

            if task_id:
                _record_backfill_task_progress(task_id, summary, item, target_total=len(rows))

    if task_id:
        _update_backfill_task(task_id, status="done", message="执行完成", finished_at=time.time())
    return summary


def _run_backfill_async(task_id: str, body: BackfillRequest) -> None:
    try:
        _run_backfill(body, task_id=task_id)
    except Exception as exc:
        logger.exception("Background integration backfill task failed: %s", task_id)
        _update_backfill_task(
            task_id,
            status="failed",
            error=str(exc),
            message=str(exc) or "后台任务失败",
            finished_at=time.time(),
        )


@router.get("/services")
def get_services():
    return {"items": list_status()}


@router.post("/services/start-all")
def start_all_services():
    return {"items": start_all()}


@router.post("/services/stop-all")
def stop_all_services():
    return {"items": stop_all()}


@router.post("/services/{name}/start")
def start_service(name: str):
    return start(name)


@router.post("/services/{name}/install")
def install_service(name: str):
    return install(name)


@router.post("/services/{name}/stop")
def stop_service(name: str):
    return stop(name)


@router.get("/services/{name}/logs")
def get_service_logs(name: str, lines: int = 400):
    try:
        return read_log(name, max_lines=max(50, min(int(lines or 400), 2000)))
    except KeyError:
        raise HTTPException(status_code=404, detail="service not found")


@router.post("/backfill/async")
def start_backfill_integrations(body: BackfillRequest):
    existing = _get_active_backfill_task()
    if existing:
        existing["reused"] = True
        return existing

    task = _create_backfill_task(body)
    thread = threading.Thread(target=_run_backfill_async, args=(task["id"], body), daemon=True)
    thread.start()
    return task


@router.get("/backfill/tasks/{task_id}")
def get_backfill_task(task_id: str):
    task = _get_backfill_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("/backfill")
def backfill_integrations(body: BackfillRequest):
    return _run_backfill(body)
