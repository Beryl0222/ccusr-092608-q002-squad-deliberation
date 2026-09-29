"""只增事件存储：业务幂等按事件标识，版本按聚合递增。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class DuplicateEvent(Exception):
    """相同事件标识再次写入；携带首次写入的事件。"""

    def __init__(self, event: Mapping[str, Any]) -> None:
        super().__init__(f"事件已存在: {event['event_id']}")
        self.event = dict(event)


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "payload": dict(self.payload),
        }


class EventStore:
    def append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> StoredEvent:
        raise NotImplementedError

    def events_for_aggregate(self, aggregate_id: str) -> list[StoredEvent]:
        raise NotImplementedError

    def all_events(self) -> list[StoredEvent]:
        raise NotImplementedError


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: list[StoredEvent] = []
        self._by_id: dict[str, StoredEvent] = {}

    def append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> StoredEvent:
        if event_id in self._by_id:
            raise DuplicateEvent(self._by_id[event_id].as_dict())
        version = sum(1 for event in self._events if event.aggregate_id == aggregate_id) + 1
        event = StoredEvent(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at,
            version=version,
            payload=dict(payload),
        )
        self._events.append(event)
        self._by_id[event_id] = event
        return event

    def events_for_aggregate(self, aggregate_id: str) -> list[StoredEvent]:
        return [event for event in self._events if event.aggregate_id == aggregate_id]

    def all_events(self) -> list[StoredEvent]:
        return list(self._events)


class JsonlEventStore(InMemoryEventStore):
    """JSONL 持久化的只增存储，重启后已提交事件仍可驱动续跑。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                self._load(line)

    def _load(self, line: str) -> None:
        raw = json.loads(line)
        event = StoredEvent(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            aggregate_type=raw["aggregate_type"],
            aggregate_id=raw["aggregate_id"],
            occurred_at=raw["occurred_at"],
            version=raw["version"],
            payload=raw["payload"],
        )
        self._events.append(event)
        self._by_id[event.event_id] = event

    def append(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> StoredEvent:
        event = super().append(event_id, event_type, aggregate_type, aggregate_id, occurred_at, payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
            handle.flush()
        return event
