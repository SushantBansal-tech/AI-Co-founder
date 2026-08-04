import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import ChannelSource, SessionFactory


async def create_source(args) -> None:
    async with SessionFactory() as session:
        existing = await session.scalar(
            select(ChannelSource).where(
                ChannelSource.public_key == args.public_key
            )
        )
        if existing:
            print(
                f"Channel source already exists: {existing.id} "
                f"({existing.public_key})"
            )
            return

        source = ChannelSource(
            business_id=args.business_id,
            channel="website",
            provider="native_form",
            public_key=args.public_key,
            name=args.name,
            active=True,
            configuration={
                "require_consent": args.require_consent,
                "require_captcha": False,
                "max_submissions_per_minute": args.rate_limit,
            },
        )
        session.add(source)
        await session.commit()
        print(f"Created website channel source: {source.id}")
        print(f"Public key: {source.public_key}")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--name", default="Website Contact Form")
    parser.add_argument("--rate-limit", type=int, default=30)
    parser.add_argument("--require-consent", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(create_source(parse_args()))
