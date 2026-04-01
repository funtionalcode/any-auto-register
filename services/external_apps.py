"""插件拉取 / 启停管理"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import yaml
from core.log_utils import read_log_tail

_ROOT = Path(__file__).resolve().parents[2]
_EXT_ROOT = _ROOT / "_ext_targets"
_LOG_ROOT = Path(__file__).resolve().parent / "external_logs"
_LOG_ROOT.mkdir(parents=True, exist_ok=True)

_REMOTE_URLS = {
    "cliproxyapi": "https://github.com/router-for-me/CLIProxyAPI.git",
    "grok2api": "https://github.com/chenyme/grok2api.git",
    "kiro-manager": "https://github.com/hj01857655/kiro-account-manager.git",
}

_KIRO_MANAGER_MSI_URL = (
    "https://github.com/hj01857655/kiro-account-manager/releases/download/"
    "v1.8.3/KiroAccountManager_1.8.3_x64_zh-CN.msi"
)
_KIRO_MANAGER_MSI = _EXT_ROOT / "KiroAccountManager_1.8.3_x64_zh-CN.msi"
_KIRO_MANAGER_EXTRACT_DIR = _EXT_ROOT / "kiro-manager-msi-extract"
_KIRO_MANAGER_EXTRACT_EXE = _KIRO_MANAGER_EXTRACT_DIR / "PFiles" / "KiroAccountManager" / "kiro-account-manager.exe"

_SERVICE_META = {
    "cliproxyapi": {
        "label": "CLIProxyAPI",
        "repo_name": "CLIProxyAPI",
        "path": "",
        "health_path": "/",
        "management_path": "/management.html",
        "port": 8317,
        "kind": "web",
    },
    "grok2api": {
        "label": "grok2api",
        "repo_name": "grok2api",
        "path": "",
        "health_path": "/health",
        "management_path": "/admin",
        "port": 8011,
        "kind": "web",
    },
    "kiro-manager": {
        "label": "Kiro Account Manager",
        "repo_name": "kiro-account-manager",
        "url": "",
        "health": "",
        "kind": "desktop",
    },
}

_PROCS: dict[str, subprocess.Popen] = {}
_LOG_FILES: dict[str, Any] = {}
_LAST_ERROR: dict[str, str] = {}
_STARTING: set[str] = set()
_LOCK = threading.Lock()
_MANAGED_SERVICE_NAMES = ("cliproxyapi", "grok2api")
_MANAGED_SERVICE_DEFAULTS = {
    "cliproxyapi": {"installed": False, "started": False},
    "grok2api": {"installed": True, "started": True},
}
_BOOTSTRAP_THREAD: threading.Thread | None = None
_BOOTSTRAP_THREAD_LOCK = threading.Lock()


def _get_setting(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _set_setting(key: str, value: str) -> None:
    try:
        from core.config_store import config_store

        config_store.set(key, str(value))
    except Exception:
        pass


def _bool_setting(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _service_state_key(name: str, field: str) -> str:
    return f"external_apps_{name}_{field}"


def _persist_service_state(name: str, installed: bool | None = None, started: bool | None = None) -> None:
    if started is True and installed is False:
        installed = True
    if started is True and installed is None:
        installed = True
    if installed is not None:
        _set_setting(_service_state_key(name, "installed"), "true" if installed else "false")
    if started is not None:
        _set_setting(_service_state_key(name, "started"), "true" if started else "false")


def _migrate_managed_service_defaults(name: str, raw_installed: str, raw_started: str) -> tuple[str, str]:
    if name != "cliproxyapi":
        return raw_installed, raw_started

    migrated_key = _service_state_key(name, "defaults_migrated_v2")
    if _bool_setting(_get_setting(migrated_key, ""), default=False):
        return raw_installed, raw_started

    normalized_installed = str(raw_installed or "").strip().lower()
    normalized_started = str(raw_started or "").strip().lower()
    if normalized_installed == "true" and normalized_started == "true" and not _repo_path(name).exists():
        _persist_service_state(name, installed=False, started=False)
        raw_installed = "false"
        raw_started = "false"

    _set_setting(migrated_key, "true")
    return raw_installed, raw_started


def _load_service_state(name: str) -> dict[str, bool]:
    if name not in _SERVICE_META:
        raise KeyError(name)

    installed_key = _service_state_key(name, "installed")
    started_key = _service_state_key(name, "started")
    raw_installed = str(_get_setting(installed_key, "") or "").strip()
    raw_started = str(_get_setting(started_key, "") or "").strip()
    raw_installed, raw_started = _migrate_managed_service_defaults(name, raw_installed, raw_started)
    managed_defaults = _MANAGED_SERVICE_DEFAULTS.get(name)

    if managed_defaults and not raw_installed and not raw_started:
        _persist_service_state(
            name,
            installed=managed_defaults["installed"],
            started=managed_defaults["started"],
        )
        return {
            "installed": managed_defaults["installed"],
            "started": managed_defaults["started"],
            "managed": True,
        }

    repo_exists = _repo_path(name).exists()
    installed = _bool_setting(raw_installed, default=repo_exists)
    started = _bool_setting(raw_started, default=False)

    updates: dict[str, bool] = {}
    if managed_defaults:
        if not raw_installed:
            updates["installed"] = installed
        if not raw_started:
            updates["started"] = started

    if started and not installed:
        installed = True
        updates["installed"] = True

    if updates:
        _persist_service_state(
            name,
            installed=updates.get("installed"),
            started=updates.get("started"),
        )

    return {
        "installed": installed,
        "started": started,
        "managed": bool(managed_defaults),
    }


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _normalize_url_host(value: str, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    candidate = text if "://" in text else f"http://{text}"
    parsed = urlsplit(candidate)
    host = parsed.hostname or parsed.netloc or parsed.path
    host = str(host or "").strip().strip("/")
    return host or default


def _normalize_url_scheme(value: str, default: str = "http") -> str:
    text = str(value or "").strip().lower()
    if text in {"http", "https"}:
        return text
    return default


def _external_apps_host() -> str:
    return _normalize_url_host(
        _get_setting("external_apps_host", "") or _get_setting("local_uri", ""),
        "127.0.0.1",
    )


def _external_apps_scheme() -> str:
    return _normalize_url_scheme(_get_setting("external_apps_scheme", "http"), "http")


def _service_meta(name: str) -> dict[str, Any]:
    meta = dict(_SERVICE_META[name])
    if meta.get("kind") != "web":
        return meta

    port = int(meta.get("port") or 0)
    external_host = _external_apps_host()
    external_scheme = _external_apps_scheme()
    internal_host = "127.0.0.1"
    path = str(meta.get("path", "") or "")
    health_path = str(meta.get("health_path", "") or "")
    management_path = str(meta.get("management_path", "") or "")

    meta["url"] = f"{external_scheme}://{external_host}:{port}{path}"
    meta["health"] = f"http://{internal_host}:{port}{health_path}"
    meta["management_url"] = (
        f"{external_scheme}://{external_host}:{port}{management_path}" if management_path else ""
    )
    return meta


def _repo_path(name: str) -> Path:
    return _EXT_ROOT / _SERVICE_META[name]["repo_name"]


def _log_path(name: str) -> Path:
    return _LOG_ROOT / f"{name}.log"


def _close_log(name: str):
    f = _LOG_FILES.pop(name, None)
    if f:
        try:
            f.close()
        except Exception:
            pass


def _open_log(name: str):
    _close_log(name)
    f = open(_log_path(name), "a", encoding="utf-8")
    _LOG_FILES[name] = f
    return f


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def _clone_repo_if_missing(name: str):
    repo = _repo_path(name)
    if repo.exists():
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", _REMOTE_URLS[name], str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )


def install(name: str, *, persist_state: bool = True) -> dict[str, Any]:
    with _LOCK:
        if name not in _SERVICE_META:
            raise KeyError(name)
        _clone_repo_if_missing(name)
    if persist_state:
        _persist_service_state(name, installed=True)
    return _status_one(name)


def _health_ok(name: str) -> bool:
    url = _service_meta(name).get("health")
    if not url:
        return False
    try:
        r = requests.get(url, timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def _find_pid_by_port(port: int) -> int | None:
    if not port:
        return None
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            creationflags=_creationflags(),
        )
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP":
            local = parts[1]
            state = parts[3].upper()
            pid = parts[4]
            if local.endswith(f":{port}") and state == "LISTENING":
                try:
                    return int(pid)
                except Exception:
                    return None
    return None


def _proc_running(name: str) -> bool:
    proc = _PROCS.get(name)
    return bool(proc and proc.poll() is None)


class _LastValueSafeLoader(yaml.SafeLoader):
    pass


def _construct_last_value_mapping(loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


_LastValueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_last_value_mapping,
)


def _kiro_known_exe_paths() -> list[str]:
    candidates: list[str] = []
    try:
        from core.config_store import config_store

        configured = str(config_store.get("kiro_manager_exe", "") or "").strip()
        if configured and Path(configured).exists():
            candidates.append(str(Path(configured).resolve()).lower())
    except Exception:
        pass

    for item in [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiroAccountManager" / "KiroAccountManager.exe",
        Path(os.environ.get("ProgramFiles", "")) / "KiroAccountManager" / "KiroAccountManager.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "kiro-account-manager" / "kiro-account-manager.exe",
        Path(os.environ.get("ProgramFiles", "")) / "kiro-account-manager" / "kiro-account-manager.exe",
        _KIRO_MANAGER_EXTRACT_EXE,
    ]:
        if item.exists():
            candidates.append(str(item.resolve()).lower())
    return candidates


def _find_desktop_pid(name: str) -> int | None:
    if name != "kiro-manager":
        return None

    target_paths = set(_kiro_known_exe_paths())

    try:
        processes = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -in @('KiroAccountManager.exe','kiro-account-manager.exe') } | "
                "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress",
            ],
            text=True,
            creationflags=_creationflags(),
        ).strip()
    except Exception:
        return None

    if not processes:
        return None

    try:
        import json

        data = json.loads(processes)
        items = data if isinstance(data, list) else [data]
        for item in items:
            pid = item.get("ProcessId")
            exe = str(item.get("ExecutablePath") or "").strip()
            if not pid:
                continue
            if not target_paths:
                return int(pid)
            if exe:
                try:
                    if str(Path(exe).resolve()).lower() in target_paths:
                        return int(pid)
                except Exception:
                    if exe.lower() in target_paths:
                        return int(pid)
    except Exception:
        return None

    return None


def _status_one(name: str) -> dict[str, Any]:
    meta = _service_meta(name)
    repo = _repo_path(name)
    desired_state = _load_service_state(name)
    proc = _PROCS.get(name)
    desktop_pid = _find_desktop_pid(name) if meta["kind"] == "desktop" else None
    running = _health_ok(name) if meta["kind"] == "web" else bool(desktop_pid or _proc_running(name))
    process_alive = _proc_running(name) if meta["kind"] == "web" else bool(desktop_pid or _proc_running(name))
    starting = name in _STARTING or (meta["kind"] == "web" and process_alive and not running)
    pid = proc.pid if proc and proc.poll() is None else desktop_pid
    if meta["kind"] == "web" and running:
        pid = _find_pid_by_port(int(meta.get("port") or 0)) or pid
    return {
        "name": name,
        "label": meta["label"],
        "repo_path": str(repo),
        "repo_exists": repo.exists(),
        "url": meta.get("url", ""),
        "management_url": meta.get("management_url", ""),
        "management_key": (
            _get_setting("cliproxyapi_management_key", "cliproxyapi")
            if name == "cliproxyapi"
            else _get_setting("grok2api_app_key", "grok2api")
            if name == "grok2api"
            else ""
        ),
        "desired_installed": desired_state["installed"],
        "desired_running": desired_state["started"],
        "managed": desired_state["managed"],
        "running": running,
        "starting": starting,
        "process_alive": process_alive,
        "pid": pid,
        "log_path": str(_log_path(name)),
        "last_error": _LAST_ERROR.get(name, ""),
        "kind": meta["kind"],
    }


def list_status() -> list[dict[str, Any]]:
    return [_status_one(name) for name in _SERVICE_META]


def read_log(name: str, max_lines: int = 400, max_bytes: int = 128 * 1024) -> dict[str, Any]:
    if name not in _SERVICE_META:
        raise KeyError(name)

    return read_log_tail(
        _log_path(name),
        max_lines=max_lines,
        max_bytes=max_bytes,
        strip_control_chars=True,
        extra={"name": name},
    )


def _find_go() -> str | None:
    candidates = [
        shutil.which("go"),
        str(Path.home() / "go" / "pkg" / "mod" / "golang.org" / "toolchain@v0.0.1-go1.24.10.windows-amd64" / "bin" / "go.exe"),
        str(Path.home() / "go" / "pkg" / "mod" / "golang.org" / "toolchain@v0.0.1-go1.24.0.windows-amd64" / "bin" / "go.exe"),
        r"C:\Program Files\Go\bin\go.exe",
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    return None


def _conda_exe() -> str | None:
    candidates = [
        shutil.which("conda"),
        r"D:\miniconda\conda3\Scripts\conda.exe",
        r"D:\miniconda\conda3\Library\bin\conda.bat",
    ]
    for item in candidates:
        if item and Path(item).exists():
            return item
    return None


def _uv_exe() -> str | None:
    candidate = shutil.which("uv")
    if candidate and Path(candidate).exists():
        return candidate
    return None


def _venv_python(repo: Path) -> Path:
    if os.name == "nt":
        return repo / ".venv" / "Scripts" / "python.exe"
    return repo / ".venv" / "bin" / "python"


def _resolve_kiro_exe() -> str | None:
    try:
        from core.config_store import config_store

        configured = str(config_store.get("kiro_manager_exe", "") or "").strip()
        if configured and Path(configured).exists():
            return configured
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiroAccountManager" / "KiroAccountManager.exe",
        Path(os.environ.get("ProgramFiles", "")) / "KiroAccountManager" / "KiroAccountManager.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "kiro-account-manager" / "kiro-account-manager.exe",
        Path(os.environ.get("ProgramFiles", "")) / "kiro-account-manager" / "kiro-account-manager.exe",
        _KIRO_MANAGER_EXTRACT_EXE,
    ]
    for item in candidates:
        if item.exists():
            return str(item)
    extracted = _ensure_kiro_extracted_exe()
    if extracted:
        return extracted
    return None


def _download_file(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _ensure_kiro_extracted_exe() -> str | None:
    if _KIRO_MANAGER_EXTRACT_EXE.exists():
        return str(_KIRO_MANAGER_EXTRACT_EXE)
    if not _KIRO_MANAGER_MSI.exists():
        _download_file(_KIRO_MANAGER_MSI_URL, _KIRO_MANAGER_MSI)
    _KIRO_MANAGER_EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "msiexec.exe",
            "/a",
            str(_KIRO_MANAGER_MSI),
            f"TARGETDIR={_KIRO_MANAGER_EXTRACT_DIR}",
            "/qn",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )
    if _KIRO_MANAGER_EXTRACT_EXE.exists():
        return str(_KIRO_MANAGER_EXTRACT_EXE)
    return None


def _ensure_grok2api_conda_env(repo: Path) -> str:
    env_name = "grok2api-313"
    conda = _conda_exe()
    if not conda:
        raise RuntimeError("未找到 conda，无法为 grok2api 自动创建 Python 3.13 环境")

    check = subprocess.run(
        [conda, "run", "--no-capture-output", "-n", env_name, "python", "--version"],
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )
    if check.returncode != 0:
        subprocess.run(
            [conda, "create", "-y", "-n", env_name, "python=3.13"],
            cwd=str(repo),
            check=True,
            creationflags=_creationflags(),
        )

    marker = repo / ".grok2api-env-ready"
    if not marker.exists():
        subprocess.run(
            [conda, "run", "--no-capture-output", "-n", env_name, "python", "-m", "pip", "install", "--upgrade", "pip"],
            cwd=str(repo),
            check=True,
            creationflags=_creationflags(),
        )
        subprocess.run(
            [conda, "run", "--no-capture-output", "-n", env_name, "python", "-m", "pip", "install", "."],
            cwd=str(repo),
            check=True,
            creationflags=_creationflags(),
        )
        marker.write_text(env_name, encoding="utf-8")
    return env_name


def _ensure_grok2api_uv_env(repo: Path) -> str:
    uv = _uv_exe()
    if not uv:
        raise RuntimeError("未找到 uv，无法为 grok2api 自动创建项目虚拟环境")

    marker = repo / ".grok2api-env-ready"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != "uv":
        subprocess.run(
            [
                uv,
                "sync",
                "--frozen",
                "--no-dev",
                "--no-install-project",
                "--python",
                "3.13",
            ],
            cwd=str(repo),
            check=True,
            creationflags=_creationflags(),
        )
        marker.write_text("uv", encoding="utf-8")
    venv_python = _venv_python(repo)
    if not venv_python.exists():
        raise RuntimeError("grok2api 的 uv 环境创建失败，未找到 .venv/python")
    return str(venv_python)


def _ensure_cliproxyapi_runtime_config(repo: Path):
    config_path = repo / "config.local.yaml"
    example_path = repo / "config.example.yaml"
    if not config_path.exists():
        shutil.copyfile(example_path, config_path)
    secret = _get_setting("cliproxyapi_management_key", "cliproxyapi")
    raw = config_path.read_text(encoding="utf-8")
    try:
        data = yaml.load(raw, Loader=_LastValueSafeLoader)
    except yaml.YAMLError:
        fallback_raw = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        data = yaml.load(fallback_raw, Loader=_LastValueSafeLoader) if fallback_raw.strip() else {}
    if not isinstance(data, dict):
        data = {}

    remote_management = data.get("remote-management")
    if not isinstance(remote_management, dict):
        remote_management = {}
    remote_management["allow-remote"] = True
    remote_management["secret-key"] = secret
    remote_management.setdefault("disable-control-panel", False)
    data["remote-management"] = remote_management

    # 兼容旧版本读取顶层 secret-key 的情况。
    data["secret-key"] = secret

    rendered = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    _atomic_write_text(config_path, rendered if rendered.endswith("\n") else rendered + "\n")


def _ensure_grok2api_runtime_config(repo: Path):
    data_dir = repo / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_file = data_dir / "config.toml"
    app_key = _get_setting("grok2api_app_key", "grok2api")
    default_config = repo / "config.defaults.toml"

    if not config_file.exists():
        if default_config.exists():
            config_file.write_text(default_config.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            config_file.write_text("[app]\n", encoding="utf-8")

    lines = config_file.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    in_app = False
    app_section_found = False
    app_key_written = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_app and not app_key_written:
                updated_lines.append(f'app_key = "{app_key}"')
                app_key_written = True
            in_app = stripped == "[app]"
            app_section_found = app_section_found or in_app
            updated_lines.append(line)
            continue

        if in_app and stripped.startswith("app_key"):
            indent = line[: len(line) - len(line.lstrip())]
            updated_lines.append(f'{indent}app_key = "{app_key}"')
            app_key_written = True
            continue

        updated_lines.append(line)

    if not app_section_found:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append("[app]")
        updated_lines.append(f'app_key = "{app_key}"')
    elif in_app and not app_key_written:
        updated_lines.append(f'app_key = "{app_key}"')

    _atomic_write_text(config_file, "\n".join(updated_lines) + "\n")


def _build_command(name: str) -> tuple[list[str], Path]:
    repo = _repo_path(name)
    if name == "cliproxyapi":
        go_exe = _find_go()
        if not go_exe:
            raise RuntimeError("未找到 go，可在设置中先安装 Go 或将 go.exe 加入 PATH")
        _ensure_cliproxyapi_runtime_config(repo)
        config_path = repo / "config.local.yaml"
        return [go_exe, "run", "./cmd/server", "-config", str(config_path)], repo

    if name == "grok2api":
        _ensure_grok2api_runtime_config(repo)
        conda = _conda_exe()
        if conda:
            env_name = _ensure_grok2api_conda_env(repo)
            return [
                conda,
                "run",
                "--no-capture-output",
                "-n",
                env_name,
                "python",
                "-m",
                "granian",
                "--interface",
                "asgi",
                "--host",
                "0.0.0.0",
                "--port",
                "8011",
                "--workers",
                "1",
                "main:app",
            ], repo

        python_exe = _ensure_grok2api_uv_env(repo)
        return [
            python_exe,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--host",
            "0.0.0.0",
            "--port",
            "8011",
            "--workers",
            "1",
            "main:app",
        ], repo

    if name == "kiro-manager":
        exe = _resolve_kiro_exe()
        if exe:
            return [exe], repo
        cargo = shutil.which("cargo")
        if not cargo:
            raise RuntimeError("未找到 Kiro Account Manager 可执行文件，且系统未安装 Rust/Cargo，无法从源码启动")
        return ["npm", "run", "tauri", "dev"], repo

    raise KeyError(name)


def start(name: str, *, persist_state: bool = True) -> dict[str, Any]:
    with _LOCK:
        if name not in _SERVICE_META:
            raise KeyError(name)
        meta = _service_meta(name)
        repo = _repo_path(name)
        if not repo.exists():
            raise RuntimeError(f"{meta['label']} 未安装，请先在插件页点击“安装”")
        if persist_state:
            _persist_service_state(name, installed=True, started=True)
        if _status_one(name)["running"] or name in _STARTING or _proc_running(name):
            return _status_one(name)
        _STARTING.add(name)

        log_file = _open_log(name)
        try:
            command, cwd = _build_command(name)
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=_creationflags(),
            )
            _PROCS[name] = proc
            _LAST_ERROR[name] = ""
        except Exception as e:
            _LAST_ERROR[name] = str(e)
            _close_log(name)
            _STARTING.discard(name)
            raise

    try:
        if meta["kind"] == "web":
            for _ in range(90):
                time.sleep(1)
                if _health_ok(name):
                    return _status_one(name)
                proc = _PROCS.get(name)
                if proc and proc.poll() is not None:
                    _LAST_ERROR[name] = f"启动失败，退出码={proc.returncode}"
                    return _status_one(name)
            _LAST_ERROR[name] = "启动超时"
        else:
            time.sleep(2)
        return _status_one(name)
    finally:
        with _LOCK:
            _STARTING.discard(name)


def stop(name: str, *, persist_state: bool = True) -> dict[str, Any]:
    with _LOCK:
        if name not in _SERVICE_META:
            raise KeyError(name)
        meta = _service_meta(name)
        if persist_state:
            _persist_service_state(name, started=False)
        _STARTING.discard(name)
        proc = _PROCS.get(name)
        port_pid = None
        desktop_pid = None
        if meta["kind"] == "web":
            port_pid = _find_pid_by_port(int(meta.get("port") or 0))
        else:
            desktop_pid = _find_desktop_pid(name)
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_creationflags(),
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
        if port_pid and (not proc or port_pid != proc.pid):
            subprocess.run(
                ["taskkill", "/PID", str(port_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creationflags(),
            )
        if desktop_pid and (not proc or desktop_pid != proc.pid):
            subprocess.run(
                ["taskkill", "/PID", str(desktop_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creationflags(),
            )
        _PROCS.pop(name, None)
        _close_log(name)
    if meta["kind"] == "web":
        for _ in range(10):
            if not _health_ok(name):
                break
            time.sleep(1)
    return _status_one(name)


def start_all() -> list[dict[str, Any]]:
    results = []
    for name in _SERVICE_META:
        try:
            if not _repo_path(name).exists():
                item = _status_one(name)
                item["last_error"] = "未安装；如需使用请先手动安装"
                results.append(item)
            else:
                results.append(start(name))
        except Exception:
            results.append(_status_one(name))
    return results


def stop_all() -> list[dict[str, Any]]:
    return [stop(name) for name in _SERVICE_META]


def ensure_managed_services_ready(names: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    target_names = [name for name in (names or _MANAGED_SERVICE_NAMES) if name in _MANAGED_SERVICE_NAMES]
    results: list[dict[str, Any]] = []

    for name in target_names:
        try:
            desired_state = _load_service_state(name)
            if desired_state["installed"] and not _repo_path(name).exists():
                install(name, persist_state=False)

            status = _status_one(name)
            if desired_state["started"] and not status["running"]:
                status = start(name, persist_state=False)
            results.append(status)
        except Exception as e:
            _LAST_ERROR[name] = str(e)
            results.append(_status_one(name))

    return results


def _bootstrap_managed_services_worker(names: tuple[str, ...]) -> None:
    global _BOOTSTRAP_THREAD
    try:
        ensure_managed_services_ready(list(names))
    finally:
        with _BOOTSTRAP_THREAD_LOCK:
            _BOOTSTRAP_THREAD = None


def ensure_managed_services_async(names: list[str] | tuple[str, ...] | None = None) -> bool:
    global _BOOTSTRAP_THREAD
    target_names = tuple(name for name in (names or _MANAGED_SERVICE_NAMES) if name in _MANAGED_SERVICE_NAMES)
    if not target_names:
        return False

    with _BOOTSTRAP_THREAD_LOCK:
        if _BOOTSTRAP_THREAD and _BOOTSTRAP_THREAD.is_alive():
            return False
        _BOOTSTRAP_THREAD = threading.Thread(
            target=_bootstrap_managed_services_worker,
            args=(target_names,),
            name="external-apps-bootstrap",
            daemon=True,
        )
        _BOOTSTRAP_THREAD.start()
    return True
