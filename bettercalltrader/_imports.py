"""Resolve the legacy top-level ``research_bot`` module.

The monolith's entry point lives in ``py/`` while this package, ``html/`` and
``.env`` stay at the repository root. Code that imports ``research_bot``
lazily (live-readiness guards, Telegram manual buys) therefore cannot rely on
the caller's ``sys.path``, so this helper adds both locations before importing.
"""

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def import_research_bot() -> Any:
    """Import ``research_bot`` from the repo root or ``py/``."""
    try:
        import research_bot

        return research_bot
    except ImportError:
        for candidate in (PROJECT_ROOT / "py", PROJECT_ROOT):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        import research_bot

        return research_bot
