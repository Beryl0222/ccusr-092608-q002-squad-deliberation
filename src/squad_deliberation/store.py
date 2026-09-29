"""领域事件存储：JSONL 追加日志，按聚合维护版本序列。

- 相同 ``event_id`` 重复追加且内容一致：幂等返回已存事件，不产生第二条。
- 相同 ``event_id`` 内容不同：抛 :class:`DuplicateConflict`。
- 每个事件的 ``version`` 必须是所属聚合的下一个连续版本，否则视为并行修改冲突。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .errors import ConcurrentModification, ContractViolation, DuplicateConflict


class EventStore:
    def __init__(self, path: str | Path | None = None, schema: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path) if path else None
        self.schema = schema
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str], int] = defaultdict(int)
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._ingest(json.loads(line))

    def _ingest(self, event: Mapping[str, Any]) -> None:
        """从磁盘重建，不做业务校验，只重建索引。"""
        event = dict(event)
        key = (event["aggregate_type"], event["aggregate_id"])
        self._events.append(event)
        self._by_id[event["event_id"]] = event
        self._versions[key] = max(self._versions[key], int(event["version"]))

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        event = dict(event)
        if event["event_id"] in self._by_id:
            existing = self._by_id[event["event_id"]]
            semantic_existing = {k: v for k, v in existing.items() if k != "version"}
            semantic_incoming = {k: v for k, v in event.items() if k != "version"}
            if semantic_existing == semantic_incoming:
                # 同一事件的重发：幂等返回已存事件，版本号以首次写入为准。
                return existing
            raise DuplicateConflict(f"事件标识 {event['event_id']} 已存在但内容不同")
        if self.schema is not None:
            issues = validate_event(event, self.schema)
            if issues:
                raise ContractViolation([f"{i.field}:{i.code}" for i in issues])
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions[key] + 1
        if int(event["version"]) != expected:
            raise ConcurrentModification(
                f"聚合 {key} 当前版本 {self._versions[key]}，收到 {event['version']}，期望 {expected}"
            )
        self._events.append(event)
        self._by_id[event["event_id"]] = event
        self._versions[key] = expected
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def all_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self._events
            if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id
        ]

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions[(aggregate_type, aggregate_id)]
