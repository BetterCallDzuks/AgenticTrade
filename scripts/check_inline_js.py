#!/usr/bin/env python3
"""Syntax-check inline ``<script>`` blocks in the dashboard HTML.

The dashboards are static files with large inline scripts, and a syntax error
there breaks the page without touching Python tests. ``node --check`` parses a
file without executing it, which is exactly what CI needs.

Usage:
    python scripts/check_inline_js.py            # skip when node is missing
    python scripts/check_inline_js.py --strict   # fail when node is missing (CI)
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML_DIR = ROOT / "html"

SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
SRC_RE = re.compile(r"(?<![\w-])src\s*=", re.IGNORECASE)
TYPE_RE = re.compile(r"\btype\s*=\s*['\"]?([^'\"\s>]+)", re.IGNORECASE)

# Executable script types. Anything else (JSON, import maps, templates) is
# data, not JavaScript, and must not be fed to node.
JS_TYPES = {
    "",
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
}
MODULE_TYPES = {"module"}
NON_JS_TYPES = {
    "application/json",
    "application/ld+json",
    "importmap",
    "text/template",
    "text/x-template",
}


def inline_scripts(path: Path) -> Iterator[Tuple[int, bool, str]]:
    """Yield ``(index, is_module, body)`` for executable inline scripts."""
    text = path.read_text(encoding="utf-8")
    for index, match in enumerate(SCRIPT_RE.finditer(text), start=1):
        attrs, body = match.group(1), match.group(2)
        if SRC_RE.search(attrs):
            continue
        type_match = TYPE_RE.search(attrs)
        mime = (type_match.group(1) if type_match else "").strip().lower()
        if mime in NON_JS_TYPES:
            continue
        is_module = mime in MODULE_TYPES
        if mime and not is_module and mime not in JS_TYPES:
            continue
        yield index, is_module, body


def check_file(node: str, path: Path) -> Tuple[int, List[str]]:
    checked = 0
    problems: List[str] = []
    for index, is_module, body in inline_scripts(path):
        if not body.strip():
            continue
        suffix = ".mjs" if is_module else ".js"
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        )
        try:
            handle.write(body)
            handle.close()
            proc = subprocess.run(
                [node, "--check", handle.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            try:
                Path(handle.name).unlink()
            except OSError:
                pass
        checked += 1
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "node --check failed").strip()
            first = detail.splitlines()[0] if detail else "node --check failed"
            problems.append(f"{path.name} script #{index}: {first}")
    return checked, problems


def html_files(paths: Iterable[str], html_dir: Path) -> List[Path]:
    explicit = [Path(item) for item in paths]
    if explicit:
        return [path for path in explicit if path.suffix.lower() == ".html"]
    return sorted(html_dir.rglob("*.html"))


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="HTML files to check (default: every .html under html/)",
    )
    parser.add_argument(
        "--html-dir",
        default=str(DEFAULT_HTML_DIR),
        help="directory searched when no paths are given",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when node is not installed instead of skipping",
    )
    args = parser.parse_args(argv)

    node = shutil.which("node")
    if not node:
        message = "node was not found; inline JS was not checked"
        if args.strict:
            print(f"::error::{message}", file=sys.stderr)
            return 1
        print(message)
        return 0

    files = html_files(args.paths, Path(args.html_dir))
    if not files:
        print("no HTML files found")
        return 0

    total_scripts = 0
    failures: List[str] = []
    for path in files:
        checked, problems = check_file(node, path)
        total_scripts += checked
        failures.extend(problems)
        status = "ok" if not problems else f"{len(problems)} failure(s)"
        print(f"{path.name}: {checked} inline script(s) — {status}")

    if failures:
        print()
        for failure in failures:
            print(f"✗ {failure}")
        print(f"\n{len(failures)} inline script(s) failed to parse")
        return 1

    print(f"\nchecked {total_scripts} inline script(s) in {len(files)} file(s): all parse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
