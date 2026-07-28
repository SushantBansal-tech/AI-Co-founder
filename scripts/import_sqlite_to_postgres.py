import asyncio
import os
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.base import Base
import app.database.models  # noqa: F401


SQLITE_PATH = Path("sales_os.db").resolve()

SQLITE_URL = (
    f"sqlite+aiosqlite:///{SQLITE_PATH.as_posix()}"
)

POSTGRES_URL = os.environ.get(
    "DATABASE_URL"
)


async def copy_table(
    sqlite_connection,
    postgres_connection,
    table,
) -> int:
    result = await sqlite_connection.execute(
        select(table)
    )

    rows = [
        dict(row._mapping)
        for row in result.fetchall()
    ]

    if not rows:
        return 0

    await postgres_connection.execute(
        insert(table),
        rows,
    )

    return len(rows)


async def main() -> None:
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(
            f"SQLite database not found: {SQLITE_PATH}"
        )

    if not POSTGRES_URL:
        raise RuntimeError(
            "DATABASE_URL must point to PostgreSQL."
        )

    if not POSTGRES_URL.startswith(
        "postgresql+asyncpg://"
    ):
        raise RuntimeError(
            "DATABASE_URL must use postgresql+asyncpg."
        )

    sqlite_engine = create_async_engine(
        SQLITE_URL
    )

    postgres_engine = create_async_engine(
        POSTGRES_URL
    )

    try:
        async with (
            sqlite_engine.connect() as sqlite_connection,
            postgres_engine.begin() as postgres_connection,
        ):
            for table in Base.metadata.sorted_tables:
                count = await copy_table(
                    sqlite_connection,
                    postgres_connection,
                    table,
                )

                print(
                    f"{table.name}: copied {count} rows"
                )

    finally:
        await sqlite_engine.dispose()
        await postgres_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())