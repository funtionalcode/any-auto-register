from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from core.db import UserModel, get_session
from core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    normalize_role,
    require_roles,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_user(user: UserModel) -> dict:
    return {
        "id": int(user.id or 0),
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _active_admin_count(session: Session) -> int:
    statement = (
        select(func.count())
        .select_from(UserModel)
        .where(UserModel.role == "admin")
        .where(UserModel.is_active == True)  # noqa: E712
    )
    return int(session.exec(statement).one() or 0)


class BootstrapRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/bootstrap/status")
def bootstrap_status(session: Session = Depends(get_session)):
    total_users = int(session.exec(select(func.count()).select_from(UserModel)).one() or 0)
    return {"bootstrapped": total_users > 0}


@router.post("/bootstrap")
def bootstrap_admin(body: BootstrapRequest, session: Session = Depends(get_session)):
    total_users = int(session.exec(select(func.count()).select_from(UserModel)).one() or 0)
    if total_users > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="系统已初始化，禁止重复 bootstrap")

    username = str(body.username or "").strip()
    password = str(body.password or "")
    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名不能为空")
    if not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码不能为空")

    user = UserModel(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        is_active=True,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    return {
        "ok": True,
        "token": create_access_token(user),
        "user": _serialize_user(user),
    }


@router.post("/login")
def login(body: LoginRequest, session: Session = Depends(get_session)):
    username = str(body.username or "").strip()
    password = str(body.password or "")
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名和密码不能为空")

    user = session.exec(select(UserModel).where(UserModel.username == username)).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已禁用")

    user.updated_at = _utcnow()
    session.add(user)
    session.commit()

    return {
        "ok": True,
        "token": create_access_token(user),
        "user": _serialize_user(user),
    }


@router.get("/me")
def get_me(current_user: UserModel = Depends(get_current_user)):
    return {"user": _serialize_user(current_user)}


@router.get("/users")
def list_users(
    _: UserModel = Depends(require_roles("admin")),
    session: Session = Depends(get_session),
):
    rows = session.exec(select(UserModel).order_by(UserModel.id.asc())).all()
    return {"items": [_serialize_user(item) for item in rows]}


@router.post("/users")
def create_user(
    body: UserCreateRequest,
    _: UserModel = Depends(require_roles("admin")),
    session: Session = Depends(get_session),
):
    username = str(body.username or "").strip()
    password = str(body.password or "")
    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名不能为空")
    if not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码不能为空")

    existing = session.exec(select(UserModel).where(UserModel.username == username)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    user = UserModel(
        username=username,
        password_hash=hash_password(password),
        role=normalize_role(body.role),
        is_active=bool(body.is_active),
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"item": _serialize_user(user)}


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdateRequest,
    current_user: UserModel = Depends(require_roles("admin")),
    session: Session = Depends(get_session),
):
    user = session.get(UserModel, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    next_role = normalize_role(body.role) if body.role is not None else user.role
    next_active = bool(body.is_active) if body.is_active is not None else user.is_active

    if current_user.id == user.id and not next_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能禁用当前登录管理员")
    if user.role == "admin" and (next_role != "admin" or not next_active) and _active_admin_count(session) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="系统至少需要保留一个可用管理员")

    if body.password is not None:
        if not str(body.password or ""):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码不能为空")
        user.password_hash = hash_password(body.password)

    user.role = next_role
    user.is_active = next_active
    user.updated_at = _utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)

    return {"item": _serialize_user(user)}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    current_user: UserModel = Depends(require_roles("admin")),
    session: Session = Depends(get_session),
):
    user = session.get(UserModel, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if current_user.id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除当前登录用户")
    if user.role == "admin" and user.is_active and _active_admin_count(session) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="系统至少需要保留一个可用管理员")

    session.delete(user)
    session.commit()
    return {"ok": True}
