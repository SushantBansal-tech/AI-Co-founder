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
                ChannelSource.channel == "whatsapp",
                ChannelSource.provider == "meta_cloud",
                ChannelSource.provider_account_id == args.phone_number_id,
            )
        )
        if existing:
            print(f"WhatsApp source already exists: {existing.id}")
            return
        source = ChannelSource(
            business_id=args.business_id,
            channel="whatsapp",
            provider="meta_cloud",
            provider_account_id=args.phone_number_id,
            public_key=args.public_key,
            name=args.name,
            configuration={
                "verify_token_env": args.verify_token_env,
                "app_secret_env": args.app_secret_env,
                "access_token_env": args.access_token_env,
                "api_version": args.api_version,
            },
        )
        session.add(source)
        await session.commit()
        print(json.dumps({"id": source.id, "public_key": source.public_key}))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--phone-number-id", required=True)
    parser.add_argument(
        "--verify-token-env", default="WHATSAPP_VERIFY_TOKEN"
    )
    parser.add_argument(
        "--app-secret-env", default="WHATSAPP_APP_SECRET"
    )
    parser.add_argument(
        "--access-token-env", default="WHATSAPP_ACCESS_TOKEN"
    )
    parser.add_argument("--api-version", default="v23.0")
    parser.add_argument("--name", default="Sales WhatsApp")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(create_source(parse_args()))
