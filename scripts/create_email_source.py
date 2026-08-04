import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import ChannelSource, SessionFactory


async def create_source(args) -> None:
    async with SessionFactory() as session:
        existing = await session.scalar(
            select(ChannelSource).where(
                ChannelSource.channel == "email",
                ChannelSource.provider == "imap",
                ChannelSource.provider_account_id == args.mailbox,
            )
        )
        if existing:
            print(f"Email source already exists: {existing.id}")
            return
        source = ChannelSource(
            business_id=args.business_id,
            channel="email",
            provider="imap",
            provider_account_id=args.mailbox.lower(),
            public_key=args.public_key,
            name=args.name,
            configuration={
                "host": args.imap_host,
                "port": args.imap_port,
                "folder": args.folder,
                "username_env": args.username_env,
                "password_env": args.password_env,
                "smtp": {
                    "host": args.smtp_host,
                    "port": args.smtp_port,
                    "username_env": args.username_env,
                    "password_env": args.password_env,
                    "from_address": args.mailbox.lower(),
                },
            },
        )
        session.add(source)
        await session.commit()
        print(json.dumps({"id": source.id, "public_key": source.public_key}))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--imap-host", required=True)
    parser.add_argument("--imap-port", type=int, default=993)
    parser.add_argument("--smtp-host", required=True)
    parser.add_argument("--smtp-port", type=int, default=465)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--username-env", default="SALES_EMAIL_USERNAME")
    parser.add_argument("--password-env", default="SALES_EMAIL_PASSWORD")
    parser.add_argument("--name", default="Sales Email Inbox")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(create_source(parse_args()))
