from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from aegis.config import get_settings
from aegis.db import models  # noqa: F401  (register tables)
from aegis.db.base import Base

target_metadata = Base.metadata
url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url


def run_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
