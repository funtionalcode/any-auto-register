"""ChatGPT 模块启用状态解析。"""

from __future__ import annotations

import json


CHATGPT_MODULE_KEYS = (
    "cpa",
    "sub2api",
    "cpa_cleanup",
    "team_manager",
    "codex_proxy",
    "smstome",
)


def _get_config_value(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def normalize_chatgpt_module_keys(values) -> list[str]:
    seen = set()
    items: list[str] = []
    for value in values or []:
        item = str(value or "").strip().lower()
        if item not in CHATGPT_MODULE_KEYS or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def parse_chatgpt_module_keys(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return normalize_chatgpt_module_keys(value)

    text = str(value or "").strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        return normalize_chatgpt_module_keys(parsed)

    return normalize_chatgpt_module_keys(
        item.strip() for item in text.split(",")
    )


def get_enabled_chatgpt_modules(default_all: bool = True) -> list[str]:
    enabled = parse_chatgpt_module_keys(_get_config_value("chatgpt_modules_enabled", ""))
    if enabled or not default_all:
        return enabled
    return list(CHATGPT_MODULE_KEYS)


def is_chatgpt_module_enabled(module_key: str, *, default_all: bool = True) -> bool:
    key = str(module_key or "").strip().lower()
    if key not in CHATGPT_MODULE_KEYS:
        return False
    return key in get_enabled_chatgpt_modules(default_all=default_all)
