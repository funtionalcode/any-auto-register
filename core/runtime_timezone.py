"""Runtime timezone helpers."""

from __future__ import annotations

import os
import time


def configure_timezone(default: str = "Asia/Shanghai") -> str:
    timezone = str(
        os.getenv("APP_TIMEZONE")
        or os.getenv("TZ")
        or default
    ).strip()
    if not timezone:
        return ""

    os.environ["TZ"] = timezone
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception:
            pass
    return timezone
