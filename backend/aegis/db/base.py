from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Numeric, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aegis.core.timeutil import ensure_utc, utcnow


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every dialect (SQLite drops tzinfo)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class Money(TypeDecorator[Decimal]):
    """Exact decimal for prices/quantities. Stored as NUMERIC(36,18)."""
    impl = Numeric(36, 18, asdecimal=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        d = Decimal(str(value))
        if dialect.name == "sqlite":  # SQLite (tests only) stores NUMERIC as float; strip binary noise
            d = d.quantize(Decimal("1e-12"))
        return d.normalize() if d != 0 else Decimal(0)


JSONType = JSON().with_variant(JSONB(), "postgresql")
PK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
