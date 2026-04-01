"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json


def _utcnow():
    return datetime.now(timezone.utc)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db")
engine = create_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = "registered"
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_extra(self) -> dict:
        return json.loads(self.extra_json or "{}")

    def set_extra(self, d: dict):
        self.extra_json = json.dumps(d, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str
    email: str
    status: str        # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


class UserModel(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Field(default="user", index=True)
    is_active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SKApiKeyModel(SQLModel, table=True):
    __tablename__ = "sk_api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(index=True)
    description: str = ""
    key_prefix: str = Field(index=True)
    key_hash: str = Field(index=True, unique=True)
    target_url: str = ""
    upstream_api_key: str = ""
    proxy_id: Optional[int] = Field(default=None, foreign_key="proxies.id", index=True)
    proxy_url: str = ""
    token_limit: int = 0
    prompt_tokens_used: int = 0
    completion_tokens_used: int = 0
    total_tokens_used: int = 0
    request_count: int = 0
    is_active: bool = True
    last_used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SKApiKeyUsageLog(SQLModel, table=True):
    __tablename__ = "sk_api_key_usage_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="sk_api_keys.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    model: str = ""
    target_url: str = ""
    proxy_url: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    success: bool = True
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ApiAccessLog(SQLModel, table=True):
    __tablename__ = "api_access_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    actor_type: str = Field(default="anonymous", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    username: str = Field(default="", index=True)
    api_key_id: Optional[int] = Field(default=None, foreign_key="sk_api_keys.id", index=True)
    api_key_name: str = ""
    api_key_prefix: str = Field(default="", index=True)
    method: str = Field(default="", index=True)
    path: str = Field(default="", index=True)
    status_code: int = Field(default=200, index=True)
    success: bool = Field(default=True, index=True)
    client_ip: str = ""
    user_agent: str = ""
    target_url: str = ""
    model: str = ""
    error: str = ""
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=_utcnow, index=True)


def save_account(account) -> 'AccountModel':
    """从 base_platform.Account 存入数据库（同平台同邮箱则更新）"""
    with Session(engine) as session:
        existing = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
        ).first()
        if existing:
            existing.password = account.password
            existing.user_id = account.user_id or ""
            existing.region = account.region or ""
            existing.token = account.token or ""
            existing.status = account.status.value
            existing.extra_json = json.dumps(account.extra or {}, ensure_ascii=False)
            existing.cashier_url = (account.extra or {}).get("cashier_url", "")
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        m = AccountModel(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id or "",
            region=account.region or "",
            token=account.token or "",
            status=account.status.value,
            extra_json=json.dumps(account.extra or {}, ensure_ascii=False),
            cashier_url=(account.extra or {}).get("cashier_url", ""),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return m


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
