from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def day_start(ts: datetime | None = None) -> datetime:
    ts = ts or utcnow()
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def ensure_utc(ts: datetime) -> datetime:
    """SQLite returns naive datetimes; normalise so comparisons never mix naive/aware."""
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
