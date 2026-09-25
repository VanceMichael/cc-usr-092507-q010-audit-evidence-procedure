"""事件存储：内存追加与 JSON 快照，支撑停机恢复。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .events import DraftEvent, Event


class EventStore:
    """追加式事件日志。同一批事件在一次 append 中整体落账。"""

    def __init__(self, events: list[Event] | None = None) -> None:
        self._events: list[Event] = list(events or [])

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(
        self,
        case_id: str,
        drafts: list[DraftEvent],
        *,
        water_level: int,
        tx_id: str,
        at: str,
    ) -> list[Event]:
        created = [
            Event(
                seq=len(self._events) + offset + 1,
                tx_id=tx_id,
                case_id=case_id,
                water_level=water_level,
                type=draft.type,
                payload=draft.payload,
                at=at,
            )
            for offset, draft in enumerate(drafts)
        ]
        self._events.extend(created)
        return created

    def dump(self, path: str | Path) -> None:
        """把事件日志写成 JSON 快照。"""
        path = Path(path)
        data = {"events": [asdict(event) for event in self._events]}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EventStore":
        """从快照恢复事件日志，供服务重放。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Event(**item) for item in data["events"]])
