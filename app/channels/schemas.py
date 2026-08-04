from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AttachmentReference(BaseModel):
    provider_file_id: str | None = None
    filename: str = Field(min_length=1, max_length=255)
    content_type: str | None = Field(default=None, max_length=100)
    size_bytes: int | None = Field(default=None, ge=0)
    download_url: str | None = None


class IncomingInquiry(BaseModel):
    business_id: str = Field(min_length=1, max_length=100)
    channel_source_id: str = Field(min_length=1, max_length=36)
    channel: Literal["website", "email", "whatsapp"]
    provider: str = Field(min_length=1, max_length=50)
    external_event_id: str = Field(min_length=1, max_length=255)
    sender_identifier: str = Field(min_length=1, max_length=255)
    sender_name: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1, max_length=10000)
    received_at: datetime
    attachments: list[AttachmentReference] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class WebsiteInquiryRequest(BaseModel):
    submission_id: str = Field(min_length=8, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    product: str | None = Field(default=None, max_length=500)
    quantity: str | None = Field(default=None, max_length=100)
    message: str = Field(min_length=1, max_length=10000)
    consent: bool = False
    captcha_token: str | None = Field(default=None, max_length=2000)
    metadata: dict = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
            raise ValueError("A valid email address is required.")
        return cleaned

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        digits = "".join(character for character in cleaned if character.isdigit())
        if len(digits) < 7 or len(digits) > 15:
            raise ValueError("A valid phone number is required.")
        return cleaned

    @model_validator(mode="after")
    def require_contact(self):
        if not self.email and not self.phone:
            raise ValueError("Either email or phone is required.")
        return self


class ChannelIngestionResponse(BaseModel):
    ingestion_id: str
    interaction_id: str
    thread_id: str
    state: dict


class ChannelJobResponse(BaseModel):
    job_id: str
    status: Literal["pending", "processing", "completed", "failed"]
    duplicate: bool = False


def website_request_to_text(request: WebsiteInquiryRequest) -> str:
    return "\n".join(
        [
            f"Name: {request.name}",
            f"Company: {request.company_name or 'Not provided'}",
            f"Email: {request.email or 'Not provided'}",
            f"Phone: {request.phone or 'Not provided'}",
            f"Product: {request.product or 'Not provided'}",
            f"Quantity: {request.quantity or 'Not provided'}",
            f"Message: {request.message}",
        ]
    )
