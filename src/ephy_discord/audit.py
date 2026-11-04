"""JSONL audit records. Message bodies and secrets are not fields of this log."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

AUDIT_FIELDS = (
    "ts",
    "event",
    "user_id",
    "channel_id",
    "guild_id",
    "model",
    "latency_ms",
    "sources",
    "cache_hit",
    "karte_status",
    "reason",
)

_SOURCE_FIELDS = ("doc_id", "title", "relative_path")


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: dict) -> None:
        safe = _sanitize(event)
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            _chmod_private(self.path)

    def forget_user(self, user_id: str) -> None:
        with self._lock:
            self._rewrite(lambda row: str(row.get("user_id", "")) != str(user_id))

    def clear(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.path.write_text("", encoding="utf-8")
            _chmod_private(self.path)

    def enforce_retention(self, cutoff_ts: str) -> None:
        with self._lock:
            self._rewrite(lambda row: str(row.get("ts", "")) > cutoff_ts)

    def read_lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with self._lock:
            text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    def _rewrite(self, keep) -> None:
        if not self.path.exists():
            return
        kept: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and keep(payload):
                kept.append(json.dumps(_sanitize(payload), ensure_ascii=False, sort_keys=True))
        body = ("\n".join(kept) + "\n") if kept else ""
        self.path.write_text(body, encoding="utf-8")
        _chmod_private(self.path)


def _sanitize(event: dict) -> dict:
    safe: dict = {}
    for key in AUDIT_FIELDS:
        if key not in event or event[key] is None:
            continue
        if key == "sources":
            safe["sources"] = [_source(item) for item in event[key] if isinstance(item, dict)]
        else:
            safe[key] = event[key]
    return safe


def _source(item: dict) -> dict:
    return {field: str(item.get(field, "")) for field in _SOURCE_FIELDS}


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
