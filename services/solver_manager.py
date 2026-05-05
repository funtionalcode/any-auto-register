"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""

import logging
_logger = logging.getLogger(__name__)

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from core.log_utils import read_log_tail

_proc: subprocess.Popen | None = None
_log_file = None
_last_error = ""
_lock = threading.Lock()


def _log_path() -> Path:
    return Path(__file__).resolve().parent / "turnstile_solver" / "solver.log"


def _solver_enabled() -> bool:
    return os.getenv("APP_ENABLE_SOLVER", "1").lower() not in {"0", "false", "no"}


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return os.getenv("SOLVER_BIND_HOST", "0.0.0.0")


def _solver_browser_type() -> str:
    # Docker 容器中默认使用 chromium（支持 headless），camoufox 需要 X server
    default = "chromium" if os.getenv("INSIDE_DOCKER") == "1" else "camoufox"
    return os.getenv("SOLVER_BROWSER_TYPE", default)


def _solver_thread() -> int:
    try:
        return max(1, int(os.getenv("SOLVER_THREAD", "1")))
    except ValueError:
        return 1


def _proc_alive() -> bool:
    global _proc
    if _proc and _proc.poll() is None:
        return True
    if _proc and _proc.poll() is not None:
        _proc = None
    return False


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def status() -> dict[str, Any]:
    alive = _proc_alive()
    return {
        "enabled": _solver_enabled(),
        "running": is_running(),
        "process_alive": alive,
        "pid": _proc.pid if alive and _proc else None,
        "url": _solver_url(),
        "bind_host": _solver_bind_host(),
        "browser_type": _solver_browser_type(),
        "thread_count": _solver_thread(),
        "log_path": str(_log_path()),
        "last_error": _last_error,
    }


def read_log(max_lines: int = 400, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    return read_log_tail(_log_path(), max_lines=max_lines, max_bytes=max_bytes)


def _close_log_file() -> None:
    global _log_file
    if _log_file:
        try:
            _log_file.close()
        except Exception:
            pass
        _log_file = None


def _process_creation_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _send_graceful_stop(proc: subprocess.Popen) -> str:
    if os.name == "nt":
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            proc.send_signal(ctrl_break)
            return "CTRL_BREAK_EVENT"
        proc.terminate()
        return "terminate"

    os.killpg(proc.pid, signal.SIGINT)
    return "SIGINT"


def start():
    global _proc, _log_file, _last_error
    with _lock:
        if not _solver_enabled():
            _logger.info("Solver disabled, skipping auto-start")
            return
        if is_running():
            _logger.info("Solver already running")
            return
        if _proc and _proc.poll() is not None:
            _proc = None
        _last_error = ""
        solver_script = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "start.py"
        )
        log_path = _log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _close_log_file()
        _log_file = open(log_path, "a", encoding="utf-8")
        _proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                solver_script,
                "--browser_type",
                _solver_browser_type(),
                "--thread",
                str(_solver_thread()),
                "--host",
                _solver_bind_host(),
                "--port",
                str(_solver_port()),
            ],
            stdout=_log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **_process_creation_kwargs(),
        )
        # 等待服务就绪（最多30s）
        for _ in range(30):
            time.sleep(1)
            if is_running():
                _logger.info("Solver started PID=%s", _proc.pid)
                return
            if _proc.poll() is not None:
                _last_error = f"启动失败，退出码={_proc.returncode}"
                _logger.error("Solver %s, log: %s", _last_error, log_path)
                _proc = None
                _close_log_file()
                return
        _last_error = "启动超时"
        _logger.error("Solver %s, log: %s", _last_error, log_path)


def stop():
    global _proc, _last_error
    with _lock:
        proc = _proc
        if proc and proc.poll() is None:
            signal_name = ""
            try:
                signal_name = _send_graceful_stop(proc)
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    signal_name = signal_name or "terminate"
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
                    signal_name = signal_name or "kill"
            _logger.info("Solver stopped (%s)", signal_name or "unknown")
        _proc = None
        _close_log_file()
        _last_error = ""


def restart_async() -> None:
    stop()
    start_async()


def start_async():
    """在后台线程启动，不阻塞主进程"""
    t = threading.Thread(target=start, daemon=True)
    t.start()
