"""Dependency-free Prometheus text exposition.

Only the small subset used by AgenticTrade is implemented: counters, gauges and
fixed-bucket histograms. The output follows the Prometheus text format 0.0.4,
so a scraper can read it without any extra runtime dependency.
"""

import math
import threading
import time
from typing import Dict, Mapping, Optional, Sequence, Tuple

DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

Labels = Mapping[str, object]
_Key = Tuple[Tuple[str, str], ...]


def _key(labels: Optional[Labels]) -> _Key:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _fmt_labels(labels: Optional[Labels]) -> str:
    if not labels:
        return ""
    rendered = ",".join(
        f'{key}="{_escape(value)}"'
        for key, value in sorted((str(k), str(v)) for k, v in labels.items())
    )
    return "{" + rendered + "}"


def _fmt_value(value: float) -> str:
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    return repr(number)


class MetricsRegistry:
    """Thread-safe registry for counters, gauges and histograms."""

    def __init__(self, buckets: Sequence[float] = DEFAULT_BUCKETS):
        self._lock = threading.Lock()
        self._values: Dict[str, Dict[_Key, float]] = {}
        self._types: Dict[str, str] = {}
        self._help: Dict[str, str] = {}
        self._histograms: Dict[str, Dict[_Key, list]] = {}
        self._buckets = tuple(sorted(float(b) for b in buckets))
        self.started_at = time.monotonic()

    def describe(self, name: str, help_text: str, metric_type: str = "gauge") -> None:
        if metric_type not in ("counter", "gauge", "histogram"):
            raise ValueError(f"unsupported metric type: {metric_type}")
        with self._lock:
            self._types[name] = metric_type
            self._help[name] = help_text

    def inc(self, name: str, labels: Optional[Labels] = None, amount: float = 1.0) -> None:
        with self._lock:
            series = self._values.setdefault(name, {})
            key = _key(labels)
            series[key] = series.get(key, 0.0) + float(amount)
            self._types.setdefault(name, "counter")
            self._help.setdefault(name, "")

    def set(self, name: str, value: float, labels: Optional[Labels] = None) -> None:
        with self._lock:
            series = self._values.setdefault(name, {})
            series[_key(labels)] = float(value)
            self._types.setdefault(name, "gauge")
            self._help.setdefault(name, "")

    def observe(self, name: str, value: float, labels: Optional[Labels] = None) -> None:
        with self._lock:
            series = self._histograms.setdefault(name, {})
            key = _key(labels)
            entry = series.get(key)
            if entry is None:
                entry = [0] * len(self._buckets) + [0.0, 0]
                series[key] = entry
            current = float(value)
            for index, boundary in enumerate(self._buckets):
                if current <= boundary:
                    entry[index] += 1
            entry[-2] += current
            entry[-1] += 1
            self._types.setdefault(name, "histogram")
            self._help.setdefault(name, "")

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._histograms.clear()

    def render(self) -> str:
        with self._lock:
            values = {
                name: dict(series) for name, series in self._values.items()
            }
            histograms = {
                name: {key: list(entry) for key, entry in series.items()}
                for name, series in self._histograms.items()
            }
            types = dict(self._types)
            help_texts = dict(self._help)

        lines = []
        for name in sorted(set(values) | set(histograms)):
            metric_type = types.get(name, "gauge")
            lines.append(f"# HELP {name} {help_texts.get(name) or name}")
            lines.append(f"# TYPE {name} {metric_type}")

            if metric_type == "histogram":
                for key, entry in sorted(histograms.get(name, {}).items()):
                    labels: Dict[str, object] = dict(key)
                    # entry[i] is already cumulative: observe() increments every
                    # bucket whose boundary is >= the observed value.
                    for boundary, count in zip(self._buckets, entry[:-2]):
                        bucket_labels: Dict[str, object] = dict(labels)
                        bucket_labels["le"] = boundary
                        lines.append(
                            f"{name}_bucket{_fmt_labels(bucket_labels)} {count}"
                        )
                    bucket_labels = dict(labels)
                    bucket_labels["le"] = "+Inf"
                    lines.append(
                        f"{name}_bucket{_fmt_labels(bucket_labels)} {entry[-1]}"
                    )
                    lines.append(f"{name}_sum{_fmt_labels(labels)} {_fmt_value(entry[-2])}")
                    lines.append(f"{name}_count{_fmt_labels(labels)} {entry[-1]}")
            else:
                for key, value in sorted(values.get(name, {}).items()):
                    lines.append(f"{name}{_fmt_labels(dict(key))} {_fmt_value(value)}")

        return "\n".join(lines) + ("\n" if lines else "")
