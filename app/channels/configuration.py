import os

from sqlalchemy import select

from app.database import ChannelSource


async def channel_configuration_errors(
    *,
    session_factory,
    email_poller_enabled: bool,
) -> list[str]:
    async with session_factory() as session:
        sources = (
            await session.execute(
                select(ChannelSource).where(
                    ChannelSource.active.is_(True)
                )
            )
        ).scalars().all()

    errors: list[str] = []
    email_sources = [
        source for source in sources if source.channel == "email"
    ]
    whatsapp_sources = [
        source for source in sources if source.channel == "whatsapp"
    ]

    if email_poller_enabled and not email_sources:
        errors.append(
            "EMAIL_POLLER_ENABLED=true but no active email source exists."
        )

    for source in email_sources:
        configuration = source.configuration or {}
        for field in ("host", "username_env", "password_env"):
            if not configuration.get(field):
                errors.append(
                    f"Email source {source.id} is missing {field}."
                )
        for env_field in ("username_env", "password_env"):
            env_name = configuration.get(env_field)
            if env_name and not os.getenv(env_name):
                errors.append(
                    f"Email source {source.id} requires environment "
                    f"variable {env_name}."
                )
        smtp = configuration.get("smtp") or {}
        if not smtp.get("host"):
            errors.append(
                f"Email source {source.id} has no SMTP host."
            )

    for source in whatsapp_sources:
        configuration = source.configuration or {}
        if not source.provider_account_id:
            errors.append(
                f"WhatsApp source {source.id} has no phone number ID."
            )
        for field, fallback in (
            ("verify_token_env", "WHATSAPP_VERIFY_TOKEN"),
            ("app_secret_env", "WHATSAPP_APP_SECRET"),
            ("access_token_env", "WHATSAPP_ACCESS_TOKEN"),
        ):
            env_name = configuration.get(field, fallback)
            if not os.getenv(env_name):
                errors.append(
                    f"WhatsApp source {source.id} requires environment "
                    f"variable {env_name}."
                )
    return errors
