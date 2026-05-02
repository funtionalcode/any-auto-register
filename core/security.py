import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from core.db import UserModel, get_session


AUTH_TOKEN_PREFIX = "aar"
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "86400") or "86400")
APP_AUTH_SECRET = (
    os.getenv("APP_AUTH_SECRET", "").strip() or "any-auto-register-dev-secret"
).encode("utf-8")
PASSWORD_HASH_NAME = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 200_000
VALID_ROLES = {"admin", "user"}

_bearer_scheme = HTTPBearer(auto_error=False)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _sign_token(payload_segment: str) -> str:
    digest = hmac.new(APP_AUTH_SECRET, payload_segment.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def hash_password(password: str) -> str:
    password_text = str(password or "")
    if not password_text:
        raise ValueError("密码不能为空")
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password_text.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    )
    return (
        f"{PASSWORD_HASH_NAME}${PASSWORD_HASH_ITERATIONS}${salt}${derived.hex()}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt, expected = str(password_hash or "").split("$", 3)
        if scheme != PASSWORD_HASH_NAME:
            return False
        iterations = int(iterations_raw)
    except (TypeError, ValueError):
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return hmac.compare_digest(derived.hex(), expected)


def create_access_token(user: UserModel) -> str:
    payload = {
        "uid": int(user.id or 0),
        "username": str(user.username or "").strip(),
        "role": str(user.role or "").strip(),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    payload_segment = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign_token(payload_segment)
    return f"{AUTH_TOKEN_PREFIX}.{payload_segment}.{signature}"


def decode_access_token(token: str) -> dict:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != AUTH_TOKEN_PREFIX:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录令牌无效")

    payload_segment = parts[1]
    signature = parts[2]
    expected_signature = _sign_token(payload_segment)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录令牌无效")

    try:
        payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录令牌无效") from exc

    expires_at = int(payload.get("exp") or 0)
    if expires_at <= int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录令牌已过期")

    return payload if isinstance(payload, dict) else {}


def normalize_role(role: str) -> str:
    value = str(role or "").strip().lower() or "user"
    if value not in VALID_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="角色无效")
    return value


def _get_bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials or str(credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer 令牌")
    return str(credentials.credentials or "").strip()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: Session = Depends(get_session),
) -> UserModel:
    token = _get_bearer_token(credentials)
    payload = decode_access_token(token)
    user_id = int(payload.get("uid") or 0)
    if user_id <= 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录令牌无效")

    user = session.get(UserModel, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    return user


def require_roles(*roles: str) -> Callable[[UserModel], UserModel]:
    allowed_roles = {normalize_role(role) for role in roles}

    def _require(current_user: UserModel = Depends(get_current_user)) -> UserModel:
        if str(current_user.role or "").strip().lower() not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限执行当前操作")
        return current_user

    return _require


def generate_sk_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


def hash_sk_api_key(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
