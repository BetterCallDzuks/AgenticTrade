import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def configure_file_logger(log_path: str = "bot_history.log") -> logging.Logger:
    logger = logging.getLogger("BetterCallTrader")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(Path(log_path), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    return logger


class JsonFormatter(logging.Formatter):
    """Minimal structured formatter: one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_json_logging(log_path: str = "bot_history.jsonl") -> logging.Logger:
    logger = logging.getLogger("BetterCallTraderJSON")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(Path(log_path), encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"fields": fields})


_audit_lock = threading.Lock()


def audit_event(log_path: str, event: str, **fields) -> Optional[dict]:
    """Append one JSONL audit record. Never raises into trading code."""
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": event,
        **fields,
    }
    try:
        path = Path(log_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        return payload
    except Exception:
        return None


def read_audit(log_path: str, limit: int = 100) -> list:
    path = Path(log_path)
    if not path.exists():
        return []
    limit = max(1, min(int(limit), 1000))
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:][::-1]


class AlertThrottle:
    """Deduplicate repeated alerts for a cooldown window."""

    def __init__(self, cooldown_sec: float = 300.0):
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._last: dict = {}

    def should_send(self, key: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        last = self._last.get(key)
        if last is not None and current - last < self.cooldown_sec:
            return False
        self._last[key] = current
        return True

    def reset(self) -> None:
        self._last.clear()
