"""account_manager - 多平台账号管理后台"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from core.db import init_db
from core.registry import load_all
from api.accounts import router as accounts_router
from api.tasks import router as tasks_router
from api.platforms import router as platforms_router
from api.proxies import router as proxies_router
from api.config import router as config_router
from api.mailbox import router as mailbox_router
from api.actions import router as actions_router
from api.integrations import router as integrations_router
from api.auth import router as auth_router
from api.sk_keys import router as sk_keys_router, openai_router, anthropic_apps_router
from core.log_utils import configure_app_logging, configure_request_logging, read_app_log, read_request_log
from core.request_logging import RequestLoggingMiddleware
from core.runtime_timezone import configure_timezone

EXPECTED_CONDA_ENV = os.getenv("APP_CONDA_ENV", "any-auto-register")
APP_LOG_PATH = configure_app_logging()
REQUEST_LOG_PATH = configure_request_logging()
configure_timezone()
logger = logging.getLogger(__name__)


def _detect_conda_env() -> str:
    conda_env = os.getenv("CONDA_DEFAULT_ENV")
    if conda_env:
        return conda_env

    prefix_parts = os.path.normpath(sys.prefix).split(os.sep)
    if "envs" in prefix_parts:
        idx = prefix_parts.index("envs")
        if idx + 1 < len(prefix_parts):
            return prefix_parts[idx + 1]
    return ""


def _print_runtime_info() -> None:
    current_env = _detect_conda_env()
    logger.info("[Runtime] Python: %s", sys.executable)
    logger.info("[Runtime] Conda Env: %s", current_env or "未检测到")
    if EXPECTED_CONDA_ENV == "docker":
        return
    if current_env and current_env != EXPECTED_CONDA_ENV:
        logger.warning(
            "当前环境为 '%s'，推荐使用 '%s' 启动，否则 Turnstile Solver 可能因依赖缺失而无法启动。",
            current_env,
            EXPECTED_CONDA_ENV,
        )
    elif not current_env:
        logger.warning(
            "未检测到 conda 环境，推荐使用 '%s' 启动，否则 Turnstile Solver 可能因依赖缺失而无法启动。",
            EXPECTED_CONDA_ENV,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_runtime_info()
    init_db()
    from services.external_apps import ensure_managed_services_async

    ensure_managed_services_async()
    load_all()
    logger.info("[OK] 数据库初始化完成")
    from core.registry import list_platforms
    logger.info("[OK] 已加载平台: %s", [p["name"] for p in list_platforms()])
    from core.scheduler import scheduler
    scheduler.start()
    from services.solver_manager import start_async
    start_async()
    yield
    from core.scheduler import scheduler as _scheduler
    _scheduler.stop()
    from services.solver_manager import stop
    stop()


app = FastAPI(title="Account Manager", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(accounts_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(platforms_router, prefix="/api")
app.include_router(proxies_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(mailbox_router, prefix="/api")
app.include_router(actions_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(sk_keys_router, prefix="/api")
app.include_router(openai_router)
app.include_router(anthropic_apps_router)


@app.get("/api/solver/status")
def solver_status():
    from services.solver_manager import status

    return status()


@app.get("/api/solver/logs")
def solver_logs(lines: int = 400):
    from services.solver_manager import read_log

    return read_log(max_lines=max(50, min(int(lines or 400), 2000)))


@app.post("/api/solver/restart")
def solver_restart():
    from services.solver_manager import restart_async, status

    restart_async()
    data = status()
    data["message"] = "重启中"
    return data


@app.get("/api/runtime/logs")
def runtime_logs(lines: int = 400):
    data = read_app_log(max_lines=max(50, min(int(lines or 400), 2000)))
    data["label"] = "后端应用日志"
    data["path"] = str(APP_LOG_PATH)
    return data


@app.get("/api/request/logs")
def request_logs(lines: int = 400):
    data = read_request_log(max_lines=max(50, min(int(lines or 400), 2000)))
    data["label"] = "接口请求日志"
    data["path"] = str(REQUEST_LOG_PATH)
    return data


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        return FileResponse(os.path.join(_static_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("APP_RELOAD", "0").lower() in {"1", "true", "yes"}
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
