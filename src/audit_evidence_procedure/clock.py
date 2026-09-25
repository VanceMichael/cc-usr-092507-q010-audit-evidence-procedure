"""时钟抽象：便于模拟停机与恢复后期限继续执行。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """生产时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟，可推进时间模拟停机。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        self._now = value
