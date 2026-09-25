"""时钟抽象。

业务代码不直接调用 ``datetime.now()``，而是注入时钟，
使停机恢复、期限届满等场景可以在测试中被精确驱动。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, delta: timedelta) -> None: ...


class SystemClock:
    """生产环境使用的 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, delta: timedelta) -> None:
        import time

        time.sleep(delta.total_seconds())


class MutableClock:
    """可手动拨快的测试时钟。"""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def sleep(self, delta: timedelta) -> None:
        self.advance(delta)


def to_iso(value: datetime) -> str:
    """统一序列化为带时区的 ISO-8601 字符串。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
