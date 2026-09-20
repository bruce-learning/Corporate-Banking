from __future__ import annotations

from collections import defaultdict
from threading import Lock


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


class MetricsRegistry:
    """低基数 Prometheus 文本指标；不记录用户正文或任何秘密。"""

    def __init__(self):
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._lock = Lock()

    def increment(self, name: str, amount: float = 1.0, **labels: str):
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._values[key] += amount

    def observe(self, name: str, value: float, **labels: str):
        self.increment(name + "_count", 1, **labels)
        self.increment(name + "_sum", value, **labels)

    def render(self) -> str:
        with self._lock:
            rows = sorted(self._values.items())
        output = []
        for (name, labels), value in rows:
            suffix = ""
            if labels:
                suffix = "{" + ",".join(f'{key}="{_escape(item)}"' for key, item in labels) + "}"
            output.append(f"{name}{suffix} {value:g}")
        return "\n".join(output) + ("\n" if output else "")
