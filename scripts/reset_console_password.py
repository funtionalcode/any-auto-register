#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select

from core.db import DATABASE_URL, UserModel, engine
from core.security import hash_password


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset a local console user's password in the configured database.",
    )
    parser.add_argument("--username", default="", help="Console username to reset")
    parser.add_argument("--password", default="", help="New password. If omitted, prompt securely in terminal")
    parser.add_argument("--role", choices=["admin", "user"], help="Optionally update the user's role")
    parser.add_argument("--enable", action="store_true", help="Enable the user after resetting the password")
    parser.add_argument("--create", action="store_true", help="Create the user when it does not exist")
    parser.add_argument("--list", action="store_true", help="List current console users and exit")
    return parser.parse_args()


def prompt_password() -> str:
    password = getpass.getpass("New password: ")
    if not password:
        raise SystemExit("Password cannot be empty")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match")
    return password


def list_users() -> int:
    with Session(engine) as session:
        rows = session.exec(select(UserModel).order_by(UserModel.id.asc())).all()

    if not rows:
        print(f"No console users found in {DATABASE_URL}")
        return 0

    print(f"Console users in {DATABASE_URL}:")
    for row in rows:
        print(
            f"- id={int(row.id or 0)} username={row.username} role={row.role} "
            f"active={'yes' if row.is_active else 'no'}"
        )
    return 0


def main() -> int:
    args = parse_args()

    if args.list:
        return list_users()

    username = str(args.username or "").strip()
    if not username:
        raise SystemExit("Provide --username, or use --list to inspect local users")

    password = str(args.password or "")
    if not password:
        password = prompt_password()

    with Session(engine) as session:
        user = session.exec(select(UserModel).where(UserModel.username == username)).first()

        if not user:
            if not args.create:
                raise SystemExit(
                    f"User '{username}' not found. Run with --create to create it locally."
                )
            user = UserModel(
                username=username,
                password_hash=hash_password(password),
                role=args.role or "admin",
                is_active=True,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            session.add(user)
            action = "created"
        else:
            user.password_hash = hash_password(password)
            if args.role:
                user.role = args.role
            if args.enable:
                user.is_active = True
            user.updated_at = _utcnow()
            session.add(user)
            action = "updated"

        session.commit()
        session.refresh(user)

    print(
        f"User {action}: username={user.username} role={user.role} "
        f"active={'yes' if user.is_active else 'no'} db={DATABASE_URL}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
