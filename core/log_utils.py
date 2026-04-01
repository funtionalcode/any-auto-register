"""日志工具：统一日志文件落盘、清洗与尾部读取。"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ORPHAN_ANSI_CODE_RE = re.compile(r"\[(?:\d{1,3}(?:;\d{1,3})*)m")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_APP_LOGGER_CONFIGURED = False


def _default_log_root() -> Path:
    configured = str(os.getenv("APP_LOG_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser()

    database_url = str(os.getenv("DATABASE_URL", "") or "").strip()
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            return Path(db_path).expanduser().resolve().parent / "logs"

    return Path(__file__).resolve().parents[1] / "logs"


def app_log_path() -> Path:
    file_name = str(os.getenv("APP_LOG_FILE", "app.log") or "").strip() or "app.log"
    return _default_log_root() / file_name


def clean_log_text(text: str, *, strip_control_chars: bool = False) -> str:
    if not text:
        return ""
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ANSI_ESCAPE_RE.sub("", cleaned)
    cleaned = _ORPHAN_ANSI_CODE_RE.sub("", cleaned)
    if strip_control_chars:
        cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned


def read_log_tail(
    path: str | Path,
    *,
    max_lines: int = 400,
    max_bytes: int = 128 * 1024,
    strip_control_chars: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_path = Path(path)
    payload = dict(extra or {})
    payload["log_path"] = str(resolved_path)

    if not resolved_path.exists():
        payload.update(
            {
                "exists": False,
                "content": "",
                "truncated": False,
                "updated_at": 0.0,
            }
        )
        return payload

    try:
        with open(resolved_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max(1, int(max_bytes or 1)))
            f.seek(start)
            raw = f.read()

        truncated = start > 0
        text = clean_log_text(
            raw.decode("utf-8", errors="ignore"),
            strip_control_chars=strip_control_chars,
        )
        if truncated:
            newline_index = text.find("\n")
            if newline_index >= 0:
                text = text[newline_index + 1 :]

        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            truncated = True

        payload.update(
            {
                "exists": True,
                "content": "\n".join(lines),
                "truncated": truncated,
                "updated_at": resolved_path.stat().st_mtime,
            }
        )
        return payload
    except Exception as e:
        payload.update(
            {
                "exists": True,
                "content": "",
                "truncated": False,
                "updated_at": 0.0,
                "error": str(e),
            }
        )
        return payload


def configure_app_logging() -> Path:
    global _APP_LOGGER_CONFIGURED
    path = app_log_path()
    if _APP_LOGGER_CONFIGURED:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    level_name = str(os.getenv("APP_LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        path,
        maxBytes=max(1024, int(os.getenv("APP_LOG_MAX_BYTES", str(5 * 1024 * 1024)) or 5 * 1024 * 1024)),
        backupCount=max(1, int(os.getenv("APP_LOG_BACKUP_COUNT", "3") or 3)),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    existing_paths = {
        str(getattr(handler, "baseFilename", "") or "")
        for handler in root_logger.handlers
    }
    if str(path) not in existing_paths:
        root_logger.addHandler(file_handler)

    if not root_logger.handlers or all(
        getattr(handler, "baseFilename", None) for handler in root_logger.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    logging.captureWarnings(True)
    _APP_LOGGER_CONFIGURED = True
    return path


def read_app_log(*, max_lines: int = 400, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    return read_log_tail(app_log_path(), max_lines=max_lines, max_bytes=max_bytes)
