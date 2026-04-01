from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from pydantic import BaseModel
from typing import Optional
from core.db import ProxyModel, get_session
from core.proxy_pool import proxy_pool
from core.proxy_utils import normalize_proxy_url

router = APIRouter(prefix="/proxies", tags=["proxies"])


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
