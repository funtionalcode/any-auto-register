"""CPA / CLIProxyAPI 目标解析。"""

from __future__ import annotations

from urllib.parse import urlsplit


def _get_config_value(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _normalize_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if "://" in text else f"http://{text}"


def _cliproxyapi_base_url() -> str:
    try:
        from services.external_apps import _service_meta

        return str(_service_meta("cliproxyapi").get("url", "") or "").strip()
    except Exception:
        return ""


def resolve_cpa_api_url(api_url: str | None = None) -> str:
    return str(api_url or _get_config_value("cpa_api_url", "") or "").strip()


def _is_local_cliproxyapi_target(api_url: str | None = None) -> bool:
    target = resolve_cpa_api_url(api_url)
    if not target:
        return False

    parsed = urlsplit(_normalize_url(target))
    host = str(parsed.hostname or parsed.netloc or parsed.path or "").strip().lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port != 8317 or not host:
        return False

    if host in {"127.0.0.1", "localhost"}:
        return True

    clip_base = _cliproxyapi_base_url()
    if not clip_base:
        return False

    clip_parsed = urlsplit(_normalize_url(clip_base))
    clip_host = str(clip_parsed.hostname or clip_parsed.netloc or clip_parsed.path or "").strip().lower()
    clip_port = clip_parsed.port or (443 if clip_parsed.scheme == "https" else 80)
    return bool(clip_host) and host == clip_host and port == clip_port


def resolve_cpa_api_key(api_key: str | None = None, api_url: str | None = None) -> str:
    explicit = str(api_key or "").strip()
    if explicit:
        return explicit

    configured = str(_get_config_value("cpa_api_key", "") or "").strip()
    if configured:
        return configured

    cliproxyapi_key = str(_get_config_value("cliproxyapi_management_key", "") or "").strip()
    if cliproxyapi_key:
        return cliproxyapi_key

    if _is_local_cliproxyapi_target(api_url):
        return "cliproxyapi"

    return ""
