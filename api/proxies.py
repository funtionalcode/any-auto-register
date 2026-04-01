import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlmodel import Session, select

from core.db import ProxyModel, engine, get_session
from core.proxy_pool import proxy_pool
from core.proxy_utils import normalize_proxy_url

router = APIRouter(prefix="/proxies", tags=["proxies"])

_proxy_test_tasks: dict[str, dict[str, Any]] = {}
_proxy_test_tasks_lock = threading.Lock()
_MAX_FINISHED_PROXY_TEST_TASKS = 50


class ProxyCreate(BaseModel):
    url: str
    region: str = ""


class ProxyBulkCreate(BaseModel):
    proxies: list[str]
    region: str = ""


class ProxyUpdate(BaseModel):
    region: str = ""


class ProxyTestRequest(BaseModel):
    url: str


class ProxyTestSavedRequest(BaseModel):
    save_region: bool = False


def _cleanup_proxy_test_tasks() -> None:
    with _proxy_test_tasks_lock:
        finished = [
            task_id
            for task_id, task in _proxy_test_tasks.items()
            if task.get("status") in {"done", "failed"}
        ]
        if len(finished) <= _MAX_FINISHED_PROXY_TEST_TASKS:
            return
        finished.sort()
        for task_id in finished[: len(finished) - _MAX_FINISHED_PROXY_TEST_TASKS]:
            _proxy_test_tasks.pop(task_id, None)


def _serialize_proxy_test_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not task:
        return None
    return {
        "id": str(task.get("id") or ""),
        "status": str(task.get("status") or "pending"),
        "message": str(task.get("message") or ""),
        "error_message": str(task.get("error_message") or ""),
        "proxy_id": task.get("proxy_id"),
        "current_region": str(task.get("current_region") or ""),
        "result": dict(task.get("result") or {}),
        "created_at": float(task.get("created_at") or 0),
        "updated_at": float(task.get("updated_at") or 0),
        "finished_at": task.get("finished_at"),
    }


def _create_proxy_test_task(*, proxy_id: int | None = None, current_region: str = "") -> dict[str, Any]:
    now = time.time()
    task_id = f"proxy_test_{int(now * 1000)}"
    task = {
        "id": task_id,
        "status": "pending",
        "message": "排队中",
        "error_message": "",
        "proxy_id": proxy_id,
        "current_region": current_region,
        "result": {},
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    with _proxy_test_tasks_lock:
        _proxy_test_tasks[task_id] = task
    _cleanup_proxy_test_tasks()
    return _serialize_proxy_test_task(task) or {}


def _get_proxy_test_task(task_id: str) -> dict[str, Any] | None:
    with _proxy_test_tasks_lock:
        return _serialize_proxy_test_task(_proxy_test_tasks.get(task_id))


def _update_proxy_test_task(task_id: str, **changes: Any) -> None:
    with _proxy_test_tasks_lock:
        task = _proxy_test_tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = time.time()


def _run_proxy_test_task(
    task_id: str,
    *,
    url: str | None = None,
    proxy_id: int | None = None,
    save_region: bool = False,
) -> None:
    try:
        _update_proxy_test_task(task_id, status="running", message="正在测试代理")

        if proxy_id is None:
            result = proxy_pool.test_proxy(str(url or ""), update_stats=False)
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "代理测试失败"))
            _update_proxy_test_task(
                task_id,
                status="done",
                message="测试完成",
                result=result,
                finished_at=time.time(),
            )
            return

        with Session(engine) as session:
            p = session.get(ProxyModel, proxy_id)
            if not p:
                raise RuntimeError("代理不存在")

            current_region = str(p.region or "").strip()
            _update_proxy_test_task(task_id, current_region=current_region)

            result = proxy_pool.test_proxy(p.url, update_stats=True)
            if not result.get("ok"):
                session.refresh(p)
                raise RuntimeError(str(result.get("error") or "代理测试失败"))

            if save_region:
                detected_region = str(result.get("region_label") or "").strip()
                if detected_region:
                    p.region = detected_region
                    session.add(p)
                    session.commit()
                    session.refresh(p)

            refreshed = session.get(ProxyModel, proxy_id)
            _update_proxy_test_task(
                task_id,
                status="done",
                message="测试完成",
                result={
                    **result,
                    "proxy": refreshed.model_dump() if refreshed else None,
                },
                current_region=current_region,
                finished_at=time.time(),
            )
    except Exception as exc:
        _update_proxy_test_task(
            task_id,
            status="failed",
            message=str(exc) or "代理测试失败",
            error_message=str(exc) or "代理测试失败",
            finished_at=time.time(),
        )


@router.get("")
def list_proxies(session: Session = Depends(get_session)):
    items = session.exec(select(ProxyModel)).all()
    return items


@router.post("")
def add_proxy(body: ProxyCreate, session: Session = Depends(get_session)):
    normalized_url = normalize_proxy_url(body.url) or body.url
    existing = session.exec(select(ProxyModel).where(ProxyModel.url == normalized_url)).first()
    if existing:
        raise HTTPException(400, "代理已存在")
    p = ProxyModel(url=normalized_url, region=body.region)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.post("/bulk")
def bulk_add_proxies(body: ProxyBulkCreate, session: Session = Depends(get_session)):
    added = 0
    for url in body.proxies:
        url = normalize_proxy_url(url) or url.strip()
        if not url:
            continue
        existing = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if not existing:
            session.add(ProxyModel(url=url, region=body.region))
            added += 1
    session.commit()
    return {"added": added}


@router.patch("/{proxy_id}")
def update_proxy(proxy_id: int, body: ProxyUpdate, session: Session = Depends(get_session)):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    p.region = str(body.region or "").strip()
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.delete("/{proxy_id}")
def delete_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    session.delete(p)
    session.commit()
    return {"ok": True}


@router.patch("/{proxy_id}/toggle")
def toggle_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    p.is_active = not p.is_active
    session.add(p)
    session.commit()
    return {"is_active": p.is_active}


@router.post("/check")
def check_proxies(background_tasks: BackgroundTasks):
    background_tasks.add_task(proxy_pool.check_all)
    return {"message": "检测任务已启动"}


@router.post("/test")
def test_proxy(body: ProxyTestRequest):
    result = proxy_pool.test_proxy(body.url, update_stats=False)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "代理测试失败")
    return result


@router.post("/test/async")
def test_proxy_async(body: ProxyTestRequest):
    normalized_url = normalize_proxy_url(body.url) or str(body.url or "").strip()
    if not normalized_url:
        raise HTTPException(400, "代理地址不能为空")
    task = _create_proxy_test_task()
    thread = threading.Thread(
        target=_run_proxy_test_task,
        args=(str(task["id"]),),
        kwargs={"url": normalized_url},
        daemon=True,
    )
    thread.start()
    return task


@router.get("/test/tasks/{task_id}")
def get_proxy_test_task_snapshot(task_id: str):
    snapshot = _get_proxy_test_task(task_id)
    if not snapshot:
        raise HTTPException(404, "代理测试任务不存在")
    return snapshot


@router.post("/{proxy_id}/test")
def test_saved_proxy(
    proxy_id: int,
    body: ProxyTestSavedRequest,
    session: Session = Depends(get_session),
):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")

    result = proxy_pool.test_proxy(p.url, update_stats=True)
    if not result.get("ok"):
        session.refresh(p)
        raise HTTPException(400, result.get("error") or "代理测试失败")

    if body.save_region:
        detected_region = str(result.get("region_label") or "").strip()
        if detected_region:
            p.region = detected_region
            session.add(p)
            session.commit()
            session.refresh(p)

    refreshed = session.get(ProxyModel, proxy_id)
    return {
        **result,
        "proxy": refreshed,
    }


@router.post("/{proxy_id}/test/async")
def test_saved_proxy_async(
    proxy_id: int,
    body: ProxyTestSavedRequest,
    session: Session = Depends(get_session),
):
    p = session.get(ProxyModel, proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")

    task = _create_proxy_test_task(proxy_id=proxy_id, current_region=str(p.region or "").strip())
    thread = threading.Thread(
        target=_run_proxy_test_task,
        args=(str(task["id"]),),
        kwargs={"proxy_id": proxy_id, "save_region": bool(body.save_region)},
        daemon=True,
    )
    thread.start()
    return task
