"""
Sub-problem: Follow-up Tracker

Responsibilities:
  1. DB model — one row per follow-up sent or scheduled
  2. Follow-up schedule — day 3 / 7 / 14 / 25 after quotation sent
  3. get_due_followups()  — which quotations need a follow-up today
  4. create_followup_record() — log a sent follow-up
  5. record_customer_reply() — save customer's response text
  6. get_followup_history() — full trail for a quotation

Design rule: schedule logic is pure deterministic arithmetic.
No LLM calls in this file.

Run:
    python 13_followup_tracker.py
"""

import uuid
import sys
import os
import asyncio
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass
from typing import Optional
from importlib import import_module

from sqlalchemy import (
    String, Text, Integer, DateTime, Boolean,
    ForeignKey, Enum as SAEnum, select, and_,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from app.database.models.followup import (
    FollowUpRecord,
    FollowUpStatus,
    FollowUpType,
)
from app.database.base import Base

sys.path.insert(0, os.path.dirname(__file__))
ia = import_module("01_Inquiry")
Base       = ia.Base
log_action = ia.log_action


# ── Enums ─────────────────────────────────────────────────────────────────

# class FollowUpStatus(str, Enum):
#     SCHEDULED             = "scheduled"
#     SENT                  = "sent"
#     CUSTOMER_REPLIED      = "customer_replied"
#     OBJECTION_DETECTED    = "objection_detected"
#     NEGOTIATION_ACTIVE    = "negotiation_active"
#     CLOSED_WON            = "closed_won"
#     CLOSED_LOST           = "closed_lost"
#     EXPIRED               = "expired"


# class FollowUpType(str, Enum):
#     REMINDER_1            = "reminder_1"      # gentle, day 3
#     REMINDER_2            = "reminder_2"      # moderate, day 7
#     REMINDER_3            = "reminder_3"      # urgent, day 14
#     VALIDITY_EXPIRY       = "validity_expiry" # final, day 25
#     OBJECTION_RESPONSE    = "objection_response"
#     NEGOTIATION_FOLLOWUP  = "negotiation_followup"


# ── DB Model ──────────────────────────────────────────────────────────────

# class FollowUpRecord(Base):
#     __tablename__ = "followup_records"

#     id: Mapped[str]            = mapped_column(String(36), primary_key=True,
#                                                 default=lambda: str(uuid.uuid4()))
#     quotation_id: Mapped[str]  = mapped_column(String(36), index=True)
#     quotation_number: Mapped[str] = mapped_column(String(30), index=True)
#     inquiry_id: Mapped[str]    = mapped_column(String(36), index=True)
#     buyer_company: Mapped[str] = mapped_column(String(255))
#     channel: Mapped[str]       = mapped_column(String(20))   # "email" | "whatsapp"
#     recipient: Mapped[str]     = mapped_column(String(255))  # email or phone

#     attempt_number: Mapped[int] = mapped_column(Integer, default=1)
#     followup_type: Mapped[str]  = mapped_column(SAEnum(FollowUpType))
#     tone: Mapped[str]           = mapped_column(String(20), default="gentle")
#     message_text: Mapped[str]   = mapped_column(Text)

#     sent_at: Mapped[Optional[datetime]]   = mapped_column(DateTime, nullable=True)
#     status: Mapped[str]  = mapped_column(SAEnum(FollowUpStatus), default=FollowUpStatus.SCHEDULED)

#     # Customer reply (filled when reply arrives)
#     customer_reply: Mapped[Optional[str]]      = mapped_column(Text, nullable=True)
#     reply_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
#     objection_type: Mapped[Optional[str]]      = mapped_column(String(50), nullable=True)

#     created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
#     updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                #   onupdate=datetime.utcnow)


# ── Follow-up schedule ────────────────────────────────────────────────────

@dataclass
class ScheduleItem:
    days_after: int        # days after quotation was sent
    attempt:    int        # attempt number (1-based)
    tone:       str        # "gentle" | "moderate" | "urgent" | "final"
    followup_type: FollowUpType
    label:      str


FOLLOW_UP_SCHEDULE: list[ScheduleItem] = [
    ScheduleItem(3,  1, "gentle",   FollowUpType.REMINDER_1,      "First reminder"),
    ScheduleItem(7,  2, "moderate", FollowUpType.REMINDER_2,      "Second reminder"),
    ScheduleItem(14, 3, "urgent",   FollowUpType.REMINDER_3,      "Third reminder"),
    ScheduleItem(25, 4, "final",    FollowUpType.VALIDITY_EXPIRY, "Validity expiry notice"),
]


def get_due_schedule_item(days_since_sent: int) -> Optional[ScheduleItem]:
    """
    Returns which follow-up is due given how many days have passed
    since the quotation was sent. Returns None if nothing is due today.
    """
    for item in FOLLOW_UP_SCHEDULE:
        if days_since_sent == item.days_after:
            return item
    return None


def days_since(sent_at: datetime) -> int:
    return (datetime.utcnow() - sent_at).days


# ── DB operations ─────────────────────────────────────────────────────────

async def get_due_followups(session: AsyncSession) -> list[dict]:
    """
    Finds all sent quotations that need a follow-up today.
    Returns list of dicts with quotation + schedule info.
    """
    # Import Quotation from whichever module defines it
    try:
        from database import Quotation, QuotationStatus
    except ImportError:
        qr = import_module("12_quotation")
        Quotation = qr.QuotationRecord
        QuotationStatus = import_module("11_quotation").QuotationStatus

    # Get all sent quotations not yet closed
    result = await session.execute(
        select(Quotation).where(
            Quotation.status == QuotationStatus.SENT.value
        )
    )
    quotations = result.scalars().all()

    due = []
    for q in quotations:
        sent_at = q.updated_at or q.created_at
        elapsed = days_since(sent_at)
        schedule = get_due_schedule_item(elapsed)
        if schedule is None:
            continue

        # Check if this attempt was already sent
        existing = await session.execute(
            select(FollowUpRecord).where(
                and_(
                    FollowUpRecord.quotation_id == q.id,
                    FollowUpRecord.attempt_number == schedule.attempt,
                )
            )
        )
        if existing.scalar_one_or_none():
            continue  # already sent this attempt

        due.append({
            "quotation_id":     q.id,
            "quotation_number": q.quotation_number,
            "buyer_company":    q.buyer_company,
            "sent_at":          sent_at,
            "days_elapsed":     elapsed,
            "schedule":         schedule,
            "draft_json":       q.draft_json,
        })

    return due


async def create_followup_record(
    session: AsyncSession,
    quotation_id: str,
    quotation_number: str,
    inquiry_id: str,
    buyer_company: str,
    channel: str,
    recipient: str,
    attempt: int,
    followup_type: FollowUpType,
    tone: str,
    message_text: str,
    business_id: str = "demo-steel-company",
    customer_id: Optional[str] = None,
    thread_id: str = "",
) -> FollowUpRecord:
    record = FollowUpRecord(
        business_id=business_id,
        customer_id=customer_id,
        thread_id=thread_id or inquiry_id,
        quotation_id=quotation_id,
        quotation_number=quotation_number,
        inquiry_id=inquiry_id,
        buyer_company=buyer_company,
        channel=channel,
        recipient=recipient,
        attempt_number=attempt,
        followup_type=followup_type,
        tone=tone,
        message_text=message_text,
        sent_at=datetime.utcnow(),
        status=FollowUpStatus.SENT,
    )
    session.add(record)
    await session.flush()
    await log_action(
        session, "followup", record.id, "followup_sent", "followup_agent",
        {"quotation_number": quotation_number, "attempt": attempt,
         "channel": channel, "recipient": recipient, "tone": tone},
    )
    await session.commit()
    return record


async def record_customer_reply(
    session: AsyncSession,
    record_id: str,
    reply_text: str,
    objection_type: Optional[str] = None,
) -> FollowUpRecord:
    result = await session.execute(
        select(FollowUpRecord).where(FollowUpRecord.id == record_id)
    )
    record = result.scalar_one()
    record.customer_reply     = reply_text
    record.reply_received_at  = datetime.utcnow()
    record.objection_type     = objection_type
    record.status = (
        FollowUpStatus.OBJECTION_DETECTED
        if objection_type and objection_type not in ("no_objection", "positive_interest")
        else FollowUpStatus.CUSTOMER_REPLIED
    )
    await log_action(
        session, "followup", record_id, "customer_reply_received", "followup_agent",
        {"objection_type": objection_type, "reply_length": len(reply_text)},
    )
    await session.commit()
    return record


async def get_followup_history(
    session: AsyncSession, quotation_number: str
) -> list[FollowUpRecord]:
    result = await session.execute(
        select(FollowUpRecord)
        .where(FollowUpRecord.quotation_number == quotation_number)
        .order_by(FollowUpRecord.attempt_number)
    )
    return result.scalars().all()


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Follow-up schedule:")
    for item in FOLLOW_UP_SCHEDULE:
        print(f"  Day {item.days_after:>2}  attempt {item.attempt}  "
              f"[{item.tone:>8}]  {item.label}")

    print("\nSchedule lookup tests:")
    for days in [1, 3, 5, 7, 10, 14, 20, 25, 30]:
        item = get_due_schedule_item(days)
        label = item.label if item else "— nothing due"
        print(f"  Day {days:>2} → {label}")
