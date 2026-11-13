"""Lightweight security helpers for the local Command Center API.

The dashboard is meant for local use. These helpers add an optional shared
token, a simple in-memory rate limit, a CSRF gate for mutating routes and a
spoof-resistant client address. Auth itself remains opt-in when the bind stays
on loopback; the server refuses a network bind without a token.
"""

import hmac
import time
from collections import deque
from typing import Deque, Dict, Optional, Set, Tuple
from urllib.parse import urlparse


def parse_allowed_origins(raw: Optional[str]) -> Set[str]:
    """Parse a comma separated ALLOWED_ORIGINS value."""
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def is_origin_allowed(origin: Optional[str], allowed: Set[str]) -> bool:
    """Return True when a browser Origin may talk to the API.

    Localhost origins on any port are accepted by default so the dashboard keeps
    working when the server runs on a custom port. Set ALLOWED_ORIGINS="*" to
    restore the old permissive behaviour.
    """
    if not origin:
        return False
    if "*" in allowed or origin in allowed:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.hostname in ("127.0.0.1", "localhost", "::1")


def is_authorized(provided: Optional[str], expected: Optional[str]) -> bool:
    """Return True when auth is disabled or the provided token matches."""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def client_ip(headers: dict, peer: Optional[str], trust_forwarded: bool = False) -> str:
    """Return the client address for rate limiting.

    ``X-Forwarded-For`` is client-controlled, so it is only honoured when the
    deployment explicitly declares a trusted reverse proxy; otherwise any
    caller could rotate spoofed addresses and bypass the limiter.
    """
    if trust_forwarded:
        forwarded = (headers or {}).get("X-Forwarded-For") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer or "unknown"


def is_mutation_allowed(headers: dict, allowed_origins: Set[str], allow_all: bool = False) -> bool:
    """Return True when a state-changing request is same-origin or non-browser.

    Cross-site POSTs are simple requests, so they skip CORS preflight and can
    reach the API when no token is configured. Browsers mark those with an
    ``Origin`` header and ``Sec-Fetch-Site: cross-site``; local scripts send
    neither, so their absence is not treated as hostile.
    """
    headers = headers or {}
    site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site in ("same-origin", "none"):
        return True
    origin = headers.get("Origin")
    if not origin:
        return True
    if allow_all:
        return True
    return is_origin_allowed(origin, allowed_origins)


class RateLimiter:
    def __init__(self, max_requests: int = 240, window_seconds: float = 60.0, max_keys: int = 2048):
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(64, int(max_keys))
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str, now: Optional[float] = None) -> Tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        current = time.monotonic() if now is None else float(now)
        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self.max_keys:
                self._evict_oldest(current)
            bucket = deque()
            self._hits[key] = bucket
        cutoff = current - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            retry_after = max(1.0, bucket[0] + self.window_seconds - current)
            return False, retry_after
        bucket.append(current)
        return True, 0.0

    def remaining(self, key: str, now: Optional[float] = None) -> int:
        current = time.monotonic() if now is None else float(now)
        bucket = self._hits.get(key) or deque()
        cutoff = current - self.window_seconds
        active = sum(1 for ts in bucket if ts > cutoff)
        return max(0, self.max_requests - active)

    def reset(self) -> None:
        self._hits.clear()

    def _evict_oldest(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) >= self.max_keys:
            oldest = min(self._hits.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
            self._hits.pop(oldest[0], None)
