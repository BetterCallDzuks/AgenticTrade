"""Small in-memory price cache shared by discovery, research and monitor loops.

This is deliberately transport-agnostic: the current provider is DexScreener
polling, but a future WebSocket feed can call `update()` with the same payload
shape without touching the call sites.
"""

import copy
import time
from collections import OrderedDict
from typing import Dict, Optional


class PriceCache:
    def __init__(self, max_entries: int = 2048, default_ttl_sec: float = 3.0):
        self.max_entries = max(1, int(max_entries))
        self.default_ttl_sec = max(0.0, float(default_ttl_sec))
        self._entries: "OrderedDict[str, dict]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def update(self, mint: str, market: dict, now: Optional[float] = None) -> None:
        if not mint or not market:
            return
        current = time.time() if now is None else float(now)
        payload = copy.deepcopy(market)
        payload["cached_at"] = current
        self._entries[mint] = payload
        self._entries.move_to_end(mint)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def get(self, mint: str, max_age_sec: Optional[float] = None, now: Optional[float] = None) -> Optional[dict]:
        payload = self._entries.get(mint)
        if payload is None:
            self.misses += 1
            return None
        current = time.time() if now is None else float(now)
        ttl = self.default_ttl_sec if max_age_sec is None else max(0.0, float(max_age_sec))
        if current - float(payload.get("cached_at", 0.0)) > ttl:
            self._entries.pop(mint, None)
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(mint)
        result = dict(payload)
        result["cache_hit"] = True
        result["cache_age_sec"] = current - float(payload.get("cached_at", current))
        return result

    def invalidate(self, mint: str) -> None:
        self._entries.pop(mint, None)

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "hitRate": (self.hits / total) if total else 0.0,
            "ttlSec": self.default_ttl_sec,
        }
