"""Run Uvicorn with a psycopg-compatible event loop on Windows."""

import asyncio
import os
import sys

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()
        )
    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
