"""Cross-platform graceful shutdown controller.

The bot runs long network waits and background tasks. Replacing the default
KeyboardInterrupt behaviour with an event lets the main loop finish the current
step, cancel its tasks and close the HTTP runner cleanly on both POSIX and
Windows.
"""

import asyncio
import signal
from typing import Any, Dict, Optional


class ShutdownController:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason: Optional[str] = None
        self._installed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._previous: Dict[int, Any] = {}

    @property
    def requested(self) -> bool:
        return self.event.is_set()

    def request(self, reason: str = "manual") -> None:
        if self.event.is_set():
            return
        self.reason = reason
        self.event.set()

    def install(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> "ShutdownController":
        if self._installed:
            return self
        self._loop = loop or asyncio.get_event_loop()
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._loop.add_signal_handler(sig, self.request, name)
                self._installed = True
                continue
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            try:
                self._previous[sig] = signal.getsignal(sig)

                def _handler(signum: int, frame: Any, label: str = name) -> None:
                    self.request(label)

                signal.signal(sig, _handler)
                self._installed = True
            except (ValueError, OSError, RuntimeError):
                continue
        return self

    def uninstall(self) -> None:
        if self._loop is not None:
            for name in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, name, None)
                if sig is None:
                    continue
                try:
                    self._loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
        for sig, previous in self._previous.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError, RuntimeError):
                pass
        self._previous.clear()
        self._installed = False
