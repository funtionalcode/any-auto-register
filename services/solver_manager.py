"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

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
        "log_path": str(_log_path()),
        "last_error": _last_error,
    }


def _clean_log_text(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def read_log(max_lines: int = 400, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    path = _log_path()
    if not path.exists():
        return {
            "log_path": str(path),
            "exists": False,
            "content": "",
            "truncated": False,
            "updated_at": 0.0,
        }

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max(1, int(max_bytes or 1)))
            f.seek(start)
            raw = f.read()

        truncated = start > 0
        text = _clean_log_text(raw.decode("utf-8", errors="ignore"))
        if truncated:
            newline_index = text.find("\n")
            if newline_index >= 0:
                text = text[newline_index + 1 :]

        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            truncated = True

        return {
            "log_path": str(path),
            "exists": True,
            "content": "\n".join(lines),
            "truncated": truncated,
            "updated_at": path.stat().st_mtime,
        }
    except Exception as e:
        return {
            "log_path": str(path),
            "exists": True,
            "content": "",
            "truncated": False,
            "updated_at": 0.0,
            "error": str(e),
        }


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
            print("[Solver] 已禁用，跳过自动启动")
            return
        if is_running():
            print("[Solver] 已在运行")
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
                print(f"[Solver] 已启动 PID={_proc.pid}")
                return
            if _proc.poll() is not None:
                _last_error = f"启动失败，退出码={_proc.returncode}"
                print(f"[Solver] {_last_error}，日志: {log_path}")
                _proc = None
                _close_log_file()
                return
        _last_error = "启动超时"
        print(f"[Solver] {_last_error}，日志: {log_path}")


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
            print(f"[Solver] 已停止 ({signal_name or 'unknown'})")
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
