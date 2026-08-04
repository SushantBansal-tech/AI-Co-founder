# app/database/engine.py

# import os

# from sqlalchemy.ext.asyncio import (
#     AsyncSession,
#     async_sessionmaker,
#     create_async_engine,
# )

# DATABASE_URL = os.environ["DATABASE_URL"]

# engine = create_async_engine(
#     DATABASE_URL,
#     pool_pre_ping=True,
#     pool_size=10,
#     max_overflow=20,
# )

# SessionFactory = async_sessionmaker(
#     bind=engine,
#     class_=AsyncSession,
#     expire_on_commit=False,
# )

import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Database configuration must be loaded before this module creates the
# process-wide engine. This keeps API, scripts, and Alembic on the same DB.
load_dotenv()

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///sales_os.db"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    DEFAULT_DATABASE_URL,
)


engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
)


SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def dispose_database_engine() -> None:
    await engine.dispose()
