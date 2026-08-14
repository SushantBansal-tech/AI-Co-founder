import argparse
import asyncio
from getpass import getpass

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()

from app.crm.auth import hash_password, normalize_user_email
from app.database import BusinessMembership, SessionFactory, User


async def create_admin(args) -> None:
    password = args.password or getpass("CRM admin password (12+ characters): ")
    normalized = normalize_user_email(args.email)
    async with SessionFactory() as session:
        user = await session.scalar(
            select(User).where(User.normalized_email == normalized)
        )
        if user is None:
            user = User(
                email=args.email.strip(),
                normalized_email=normalized,
                display_name=args.display_name.strip(),
                password_hash=hash_password(password),
            )
            session.add(user)
            await session.flush()
        membership = await session.scalar(
            select(BusinessMembership).where(
                BusinessMembership.business_id == args.business_id,
                BusinessMembership.user_id == user.id,
            )
        )
        if membership is None:
            membership = BusinessMembership(
                business_id=args.business_id,
                user_id=user.id,
                role="admin",
            )
            session.add(membership)
        else:
            membership.role = "admin"
            membership.status = "active"
        await session.commit()
        print(f"CRM admin ready: {user.email} for {args.business_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first CRM administrator.")
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--password", help="Omit to enter securely at the prompt.")
    args = parser.parse_args()
    asyncio.run(create_admin(args))


if __name__ == "__main__":
    main()
