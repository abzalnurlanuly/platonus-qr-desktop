"""Bounded, thread-safe diagnostics for terminal and localhost clients."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Lock


Scalar = str | int | bool | None
DiagnosticEntry = dict[str, Scalar]


def _clean(value: str) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in value[:512]).split())


class DiagnosticLog:
    """Record safe event metadata. Callers must omit secrets and raw payloads."""

    def __init__(self, *, on_entry: Callable[[DiagnosticEntry], None] | None = None) -> None:
        self._entries: deque[DiagnosticEntry] = deque(maxlen=100)
        self._lock = Lock()
        self._on_entry = on_entry

    def record(self, event: str, **fields: Scalar) -> None:
        entry: DiagnosticEntry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": _clean(event),
        }
        for key, value in fields.items():
            if key in entry or value is None:
                continue
            entry[key] = _clean(value) if isinstance(value, str) else value
        with self._lock:
            self._entries.append(entry)
        if self._on_entry is not None:
            try:
                self._on_entry(dict(entry))
            except (OSError, UnicodeError):
                pass

    def snapshot(self) -> list[DiagnosticEntry]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
