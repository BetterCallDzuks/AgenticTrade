"""LM Studio process/model control through the ``lms`` CLI.

The OpenAI-compatible HTTP API can list models but cannot reliably unload them,
and loading a second model while a large one is resident is the usual way a
local research loop runs out of VRAM. The ``lms`` CLI exposes both, so this
module wraps it with a timeout, no shell, and a JSON-first parser.

Everything degrades to ``available: False`` when the CLI is missing or the
server is stopped: managing local models is a convenience, never a hard
dependency of the bot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT_SEC = 20.0


def lms_binary() -> Optional[str]:
    """Return the ``lms`` executable path, or None when it is not installed."""
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / "lms"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _run(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    binary = lms_binary()
    if not binary:
        return {"ok": False, "available": False, "detail": "lms CLI not found", "stdout": "", "stderr": ""}
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user string splitting
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "available": True,
            "detail": f"lms {' '.join(args)} timed out after {timeout:g}s",
            "stdout": "",
            "stderr": "",
        }
    except OSError as exc:
        return {"ok": False, "available": True, "detail": f"lms failed to start: {exc}", "stdout": "", "stderr": ""}
    return {
        "ok": completed.returncode == 0,
        "available": True,
        "detail": (completed.stderr or completed.stdout or "").strip()[:400],
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "returncode": completed.returncode,
    }


def _first_str(item: Any, *keys: str) -> str:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_ps(payload: str) -> List[Dict[str, Any]]:
    """Normalise ``lms ps --json`` output, tolerating plain-text fallbacks."""
    text = (payload or "").strip()
    if not text:
        return []
    data: Any = None
    start = min((pos for pos in (text.find("["), text.find("{")) if pos >= 0), default=-1)
    if start >= 0:
        try:
            data = json.loads(text[start:])
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        data = data.get("models") or data.get("data") or []
    if isinstance(data, list):
        models: List[Dict[str, Any]] = []
        for item in data:
            identifier = _first_str(item, "identifier", "id", "modelKey", "model")
            if not identifier:
                continue
            models.append(
                {
                    "identifier": identifier,
                    "modelKey": _first_str(item, "modelKey", "model") or identifier,
                    "state": _first_str(item, "state", "status") or "loaded",
                    "publisher": _first_str(item, "publisher") or None,
                    "params": _first_str(item, "paramsString") or None,
                    "sizeBytes": item.get("sizeBytes") if isinstance(item, dict) else None,
                    "contextLength": item.get("contextLength") if isinstance(item, dict) else None,
                    "maxContextLength": item.get("maxContextLength") if isinstance(item, dict) else None,
                    "parallel": item.get("parallel") if isinstance(item, dict) else None,
                    "ttlMs": item.get("ttlMs") if isinstance(item, dict) else None,
                    "lastUsedTime": item.get("lastUsedTime") if isinstance(item, dict) else None,
                    "vision": bool(item.get("vision")) if isinstance(item, dict) else False,
                }
            )
        return models
    # Plain text fallback: only when the output really is the `lms ps` table
    # (at least one line with several columns). Anything else is an error
    # message, and returning it as a "loaded model" would be a lie.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tabular = "IDENTIFIER" in text.upper() or any(len(line.split()) >= 3 for line in lines)
    if not tabular:
        return []
    return [
        {"identifier": line, "modelKey": line, "state": "unknown", "sizeBytes": None, "contextLength": None}
        for line in lines
        if not line.lower().startswith(("model", "——", "identifier"))
    ]


def loaded_models(timeout: float = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    """Return every model currently loaded by the local server."""
    result = _run(["ps", "--json"], timeout=timeout)
    if not result["available"]:
        return {"available": False, "reachable": False, "models": [], "detail": result["detail"]}
    models = parse_ps(result["stdout"]) if result["ok"] else []
    return {
        "available": True,
        "reachable": result["ok"],
        "models": models,
        "detail": result["detail"],
    }


def unload(model: str = "", all_models: bool = False, timeout: float = 60.0) -> Dict[str, Any]:
    """Unload one model by identifier, or every loaded model."""
    identifier = (model or "").strip()
    if not all_models and not identifier:
        return {"ok": False, "detail": "model or all=true is required", "unloaded": []}
    args = ["unload", "--all"] if all_models else ["unload", identifier]
    result = _run(args, timeout=timeout)
    return {
        "ok": bool(result["ok"]),
        "detail": result["detail"],
        "unloaded": ["*"] if all_models and result["ok"] else ([identifier] if result["ok"] else []),
        "available": result["available"],
    }


def load(model: str, timeout: float = 300.0, gpu: str = "", context_length: Optional[int] = None) -> Dict[str, Any]:
    """Load one model, JIT-style. Used by the eval harness between candidates."""
    identifier = (model or "").strip()
    if not identifier:
        return {"ok": False, "detail": "model is required", "available": True}
    args = ["load", identifier, "--yes"]
    if gpu:
        args += ["--gpu", str(gpu)]
    if context_length:
        args += ["--context-length", str(int(context_length))]
    result = _run(args, timeout=timeout)
    return {"ok": bool(result["ok"]), "detail": result["detail"], "available": result["available"]}


def _max_loaded(env: Optional[Dict[str, str]] = None) -> int:
    source = os.environ if env is None else env
    try:
        return max(0, int(str(source.get("LMS_MAX_LOADED", "1")).strip() or 1))
    except (TypeError, ValueError):
        return 1


def enforce_max_loaded(
    keep: str,
    max_loaded: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Unload every loaded model except ``keep`` when over ``LMS_MAX_LOADED``.

    ``keep`` is matched against both the identifier and the model key, because
    LM Studio reports ``qwen3.5-0.8b-uncensored`` as the key but may add a
    ``:2`` instance suffix to the identifier.
    """
    limit = _max_loaded(env) if max_loaded is None else max(0, int(max_loaded))
    state = loaded_models()
    if not state.get("available") or not state.get("reachable"):
        return {"applied": False, "limit": limit, "unloaded": [], "detail": state.get("detail", "")}
    current = state.get("models") or []
    keep_key = (keep or "").strip()
    if limit <= 0 or not current:
        return {"applied": True, "limit": limit, "unloaded": [], "detail": ""}
    extras = []
    for item in current:
        identifier = str(item.get("identifier") or "")
        model_key = str(item.get("modelKey") or "")
        if keep_key and (identifier == keep_key or model_key == keep_key or identifier.split(":")[0] == keep_key):
            continue
        extras.append(identifier)
    # Unload until at most ``limit`` models stay resident.
    keep_count = len(current) - len(extras)
    allowed_extras = max(0, limit - keep_count)
    to_unload = extras[: len(extras) - allowed_extras] if len(extras) > allowed_extras else []
    unloaded: List[str] = []
    detail = ""
    for identifier in to_unload:
        result = unload(identifier)
        if result.get("ok"):
            unloaded.append(identifier)
        else:
            detail = result.get("detail", "")
    return {"applied": True, "limit": limit, "unloaded": unloaded, "detail": detail}
