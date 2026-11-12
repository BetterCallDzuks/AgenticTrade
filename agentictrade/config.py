import os
from typing import Optional


def env_float(key: str, default: float) -> float:
    try:
        raw = os.environ.get(key)
        return float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def env_int(key: str, default: int) -> int:
    try:
        raw = os.environ.get(key)
        return int(float(raw)) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        return int(default)


def env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")