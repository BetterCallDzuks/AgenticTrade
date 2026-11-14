"""Quiet parsing for ``.env`` files.

``python-dotenv`` logs one warning per unparsable line. A human-maintained
``.env`` with documentation separators therefore prints a wall of
"could not parse statement" lines on every start, which hides a real mistake.
This module parses the same file silently and returns the ignored line numbers
so ``doctor`` can report them once, deliberately.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

_DOTENV_LOGGER = "dotenv.main"
_LINE_RE = re.compile(r"line (\d+)")

PathLike = Union[str, Path]


@dataclass(frozen=True)
class ParsedEnv:
    values: Dict[str, str]
    messages: List[str] = field(default_factory=list)
    ignored_lines: List[int] = field(default_factory=list)


def _fallback_parse(path: Path) -> ParsedEnv:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return ParsedEnv(values)


def parse_env_file(path: PathLike) -> ParsedEnv:
    """Parse ``path`` without printing python-dotenv warnings.

    Returns the parsed values plus the captured warning messages and the line
    numbers they refer to. Missing files yield an empty result.
    """
    resolved = Path(path)
    if not resolved.exists():
        return ParsedEnv({})

    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv is a required dependency
        return _fallback_parse(resolved)

    logger = logging.getLogger(_DOTENV_LOGGER)
    messages: List[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - handler
            messages.append(record.getMessage())

    handler = _Capture()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        raw = dotenv_values(dotenv_path=resolved)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    values = {str(key): str(value) for key, value in raw.items() if value is not None}
    ignored = sorted(
        {
            int(match.group(1))
            for message in messages
            for match in _LINE_RE.finditer(message)
        }
    )
    return ParsedEnv(values, messages, ignored)


def load_env_values(path: PathLike) -> Dict[str, str]:
    """Return only the parsed key/value pairs, quietly."""
    return parse_env_file(path).values


def describe_ignored_lines(parsed: ParsedEnv, limit: int = 5) -> Optional[str]:
    """Render a short, stable summary of ignored lines for diagnostics."""
    if not parsed.ignored_lines:
        return None
    shown = ", ".join(str(line) for line in parsed.ignored_lines[:limit])
    suffix = "" if len(parsed.ignored_lines) <= limit else f", +{len(parsed.ignored_lines) - limit} more"
    return f"line(s) {shown}{suffix}"
