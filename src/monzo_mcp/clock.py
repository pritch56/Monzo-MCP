"""The one place this package reads the wall clock, so tests can freeze it."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

_source: Callable[[], float] = time.time


def set_clock(source: Callable[[], float] | None) -> None:
    """Test hook: replace the time source (unix seconds), or restore the real clock with None."""
    global _source
    _source = time.time if source is None else source


def now() -> float:
    """Current time in unix seconds."""
    return _source()


def utcnow() -> datetime:
    return datetime.fromtimestamp(now(), timezone.utc)


def london_today() -> date:
    """Today's calendar date in Europe/London."""
    return utcnow().astimezone(LONDON).date()
