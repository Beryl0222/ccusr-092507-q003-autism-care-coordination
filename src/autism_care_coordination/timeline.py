"""可回溯事件时间线：仅追加、幂等键、跨方并发约束。

时间线以事件实际发生时间（occurred_at）排序回放，版本号只表示追加顺序，
因此迟到的家庭补记不会改变既有事件的语义。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping, Optional


class ContentMismatch(Exception):
    """同一幂等标识再次出现但业务内容已经变化。"""

    def __init__(self, key: str, original: Mapping[str, Any], incoming: Mapping[str, Any]) -> None:
        super().__init__(f"幂等标识 {key} 的内容与首次上传不一致")
        self.key = key
        self.original = original
        self.incoming = incoming


def business_fingerprint(event: Mapping[str, Any]) -> str:
    """同一业务内容的稳定指纹（忽略发生时间与追加版本）。"""
    core = {"event_type": event["event_type"], "aggregate_id": event["aggregate_id"], "payload": event["payload"]}
    rendered = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class Timeline:
    """进程内追加日志；可选持久化为 JSON 数组，恢复后幂等与提醒状态继续有效。"""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        self._idempotency: dict[str, str] = {}
        self._fingerprints: dict[str, str] = {}
        self.lock = threading.RLock()
        if self._path and self._path.exists():
            for event in json.loads(self._path.read_text(encoding="utf-8")):
                self._events.append(event)
                key = event.get("_idempotency_key")
                if key:
                    self._idempotency[key] = event["event_id"]
                    self._fingerprints[key] = business_fingerprint(event)

    @property
    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(event) for event in self._events]

    def persist(self, path: Optional[str | Path] = None) -> None:
        if path is not None:
            self._path = Path(path)
        if not self._path:
            return
        with self.lock:
            self._path.write_text(
                json.dumps(self._events, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def append(
        self, event: dict[str, Any], idempotency_key: Optional[str] = None
    ) -> tuple[str, dict[str, Any]]:
        """返回 (stored|duplicate, 事件)。内容冲突抛出 ContentMismatch。"""
        with self.lock:
            if idempotency_key is not None:
                existing_id = self._idempotency.get(idempotency_key)
                if existing_id is not None:
                    existing = next(event for event in self._events if event["event_id"] == existing_id)
                    fingerprint = business_fingerprint(event)
                    if self._fingerprints[idempotency_key] != fingerprint:
                        raise ContentMismatch(idempotency_key, existing, event)
                    return "duplicate", dict(existing)

            if any(existing["event_id"] == event["event_id"] for existing in self._events):
                raise ValueError(f"事件标识重复: {event['event_id']}")

            stored = dict(event)
            if idempotency_key is not None:
                stored["_idempotency_key"] = idempotency_key
                self._idempotency_set(idempotency_key, stored)
            self._events.append(stored)
            return "stored", dict(stored)

    def _idempotency_set(self, key: str, event: Mapping[str, Any]) -> None:
        self._idempotency[key] = event["event_id"]
        self._fingerprints[key] = business_fingerprint(event)

    def forget_key_marker(self, idempotency_key: str) -> None:
        self._idempotency.pop(idempotency_key, None)
        self._fingerprints.pop(idempotency_key, None)

    def next_version(self, aggregate_id: str) -> int:
        with self.lock:
            return sum(1 for event in self._events if event["aggregate_id"] == aggregate_id)
