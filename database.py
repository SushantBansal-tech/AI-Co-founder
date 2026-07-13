"""
database.py — single source of truth for every SQLAlchemy model.

All agents import models from HERE, not from their own files.
One Base, one engine, one create_all().

Usage:
    from database import get_session, Lead, Customer, Quotation ...
    from database import create_all_tables
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, AsyncGenerator

from sqlalchemy import (
    String, Text, Numeric, Integer, Date, DateTime, Boolean,
    ForeignKey, JSON, Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession, AsyncEngine,
)

from settings import DATABASE_URL


# ── Single shared Base ────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Engine + session factory (created once on startup) ───────────────────

_engine: Optional[AsyncEngine] = None
_SessionFactory = None


def init_db(database_url: str = DATABASE_URL):
    global _engine, _SessionFactory
    _engine = create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        # For Postgres in prod, add:
        # pool_size=10, max_overflow=20
    )
    _SessionFactory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )


async def create_all_tables():
    """Call once at app startup."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a session per request."""
    async with _SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ═══════════════════════════════════════════════════════════════════
# ENUM DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

class InquirySource(str, Enum):
    EMAIL    = "email"
    WHATSAPP = "whatsapp"
    WEB_FORM = "web_form"
    CRM      = "crm"
    UPLOADED_DOCUMENT = "uploaded_document"

class LeadStatus(str, Enum):
    AWAITING_INFO = "awaiting_info"
    NEW           = "new"
    IN_PROGRESS   = "in_progress"
    WON           = "won"
    LOST          = "lost"

class CustomerType(str, Enum):
    NEW      = "new"
    EXISTING = "existing"

class PaymentBehavior(str, Enum):
    EXCELLENT = "excellent"
    GOOD      = "good"
    AVERAGE   = "average"
    POOR      = "poor"
    UNKNOWN   = "unknown"

class OrderStatus(str, Enum):
    DELIVERED  = "delivered"
    IN_TRANSIT = "in_transit"
    CANCELLED  = "cancelled"
    PENDING    = "pending"

class QuotationStatus(str, Enum):
    DRAFT            = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED         = "approved"
    SENT             = "sent"
    REJECTED         = "rejected"

class ApprovalStatus(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class DocType(str, Enum):
    PRODUCT_CATALOG    = "product_catalog"
    PRICE_LIST         = "price_list"
    RM_COST            = "rm_cost"
    TRANSPORT          = "transport"
    DISCOUNT_POLICY    = "discount_policy"
    MARGIN_RULES       = "margin_rules"
    GST_RATES          = "gst_rates"
    INVENTORY          = "inventory"
    PRODUCTION_CAP     = "production_capacity"
    DELIVERY_ZONES     = "delivery_zones"
    PAYMENT_TERMS      = "payment_terms"
    QUOTATION_TEMPLATE = "quotation_template"
    TNC                = "terms_and_conditions"
    CUSTOMER_CRM       = "customer_crm"


# ═══════════════════════════════════════════════════════════════════
# TABLE 1 — uploaded_documents
# Every document the business owner uploads is recorded here.
# Agents call DocumentManager.get(doc_type) which reads from this table.
# ═══════════════════════════════════════════════════════════════════

class UploadedDocument(Base):
    __tablename__ = "uploaded_documents"

    id: Mapped[str]      = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    doc_type: Mapped[str] = mapped_column(SAEnum(DocType), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(512))  # local path or S3 key
    file_size_kb: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # only 1 active per type
    parsed_successfully: Mapped[bool] = mapped_column(Boolean, default=False)
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(100), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════
# TABLE 2 — leads
# ═══════════════════════════════════════════════════════════════════

class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str]       = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    inquiry_id: Mapped[str] = mapped_column(String(36), index=True, unique=True)
    source: Mapped[str]   = mapped_column(SAEnum(InquirySource))
    sender_identifier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    customer_name: Mapped[Optional[str]]      = mapped_column(String(255), nullable=True)
    company_name: Mapped[Optional[str]]       = mapped_column(String(255), nullable=True)
    contact_person: Mapped[Optional[str]]     = mapped_column(String(255), nullable=True)
    product_requested: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    quantity: Mapped[Optional[str]]           = mapped_column(String(100), nullable=True)
    specifications: Mapped[Optional[str]]     = mapped_column(Text, nullable=True)
    delivery_location: Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    delivery_date: Mapped[Optional[str]]      = mapped_column(String(100), nullable=True)
    payment_expectation: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[str]       = mapped_column(SAEnum(LeadStatus), default=LeadStatus.NEW)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[str]     = mapped_column(Text)

    # Populated after each agent stage completes
    requirement_summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    qualification_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feasibility_result_json: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    pricing_result_json: Mapped[Optional[str]]       = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════
# TABLE 3 — customers
# ═══════════════════════════════════════════════════════════════════

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str]       = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    company_name: Mapped[str]  = mapped_column(String(255), index=True)
    contact_person: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    city: Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    gstin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    credit_limit: Mapped[Decimal]       = mapped_column(Numeric(15, 2), default=0)
    outstanding_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    payment_behavior: Mapped[str]       = mapped_column(SAEnum(PaymentBehavior), default=PaymentBehavior.UNKNOWN)
    notes: Mapped[Optional[str]]        = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    orders:     Mapped[list["OrderHistory"]]     = relationship("OrderHistory",     back_populates="customer")
    quotations: Mapped[list["QuotationHistory"]] = relationship("QuotationHistory", back_populates="customer")
    payments:   Mapped[list["PaymentRecord"]]    = relationship("PaymentRecord",    back_populates="customer")


class OrderHistory(Base):
    __tablename__ = "order_history"
    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    order_number: Mapped[str] = mapped_column(String(50))
    product: Mapped[str]      = mapped_column(String(255))
    quantity: Mapped[str]     = mapped_column(String(100))
    order_value: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    status: Mapped[str]       = mapped_column(SAEnum(OrderStatus))
    order_date: Mapped[datetime] = mapped_column(DateTime)
    customer: Mapped["Customer"] = relationship("Customer", back_populates="orders")


class QuotationHistory(Base):
    __tablename__ = "quotation_history"
    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    quotation_number: Mapped[str]   = mapped_column(String(50))
    product: Mapped[str]            = mapped_column(String(255))
    quoted_value: Mapped[Decimal]   = mapped_column(Numeric(15, 2))
    status: Mapped[str]             = mapped_column(String(20))
    quotation_date: Mapped[datetime] = mapped_column(DateTime)
    lost_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    customer: Mapped["Customer"] = relationship("Customer", back_populates="quotations")


class PaymentRecord(Base):
    __tablename__ = "payment_records"
    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    invoice_number: Mapped[str]  = mapped_column(String(50))
    invoice_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    due_date: Mapped[datetime]   = mapped_column(DateTime)
    paid_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delay_days: Mapped[int]      = mapped_column(Integer, default=0)
    customer: Mapped["Customer"] = relationship("Customer", back_populates="payments")


# ═══════════════════════════════════════════════════════════════════
# TABLE 4 — quotations
# ═══════════════════════════════════════════════════════════════════

class Quotation(Base):
    __tablename__ = "quotations"

    id: Mapped[str]              = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    quotation_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    inquiry_id: Mapped[str]      = mapped_column(String(36), ForeignKey("leads.inquiry_id"), index=True)
    status: Mapped[str]          = mapped_column(SAEnum(QuotationStatus), default=QuotationStatus.DRAFT)
    buyer_company: Mapped[str]   = mapped_column(String(255))
    total_inc_gst: Mapped[float] = mapped_column(default=0.0)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_json: Mapped[str]      = mapped_column(Text)    # full QuotationDraft JSON
    html_content: Mapped[str]    = mapped_column(Text)    # rendered HTML
    approved_by: Mapped[Optional[str]]      = mapped_column(String(100), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_via: Mapped[Optional[str]]         = mapped_column(String(50), nullable=True)
    sent_to: Mapped[Optional[str]]          = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════
# TABLE 5 — human_approval_requests
# Pipeline pauses here; resumes when status changes.
# ═══════════════════════════════════════════════════════════════════

class HumanApprovalRequest(Base):
    __tablename__ = "human_approval_requests"

    id: Mapped[str]         = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    inquiry_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str]      = mapped_column(String(50))   # "pricing", "quotation", "feasibility" …
    reasons: Mapped[list]   = mapped_column(JSON, default=list)
    context_json: Mapped[str] = mapped_column(Text)       # the full result object that needs approval
    status: Mapped[str]     = mapped_column(SAEnum(ApprovalStatus), default=ApprovalStatus.PENDING)
    reviewed_by: Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    review_note: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ═══════════════════════════════════════════════════════════════════
# TABLE 6 — audit_logs  (generic — all agents write here)
# ═══════════════════════════════════════════════════════════════════

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str]   = mapped_column(String(36), index=True)
    action: Mapped[str]      = mapped_column(String(100))
    actor: Mapped[str]       = mapped_column(String(100))
    details: Mapped[dict]    = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════
# TABLE 7 — agent_runs  (one row per full pipeline execution)
# ═══════════════════════════════════════════════════════════════════

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str]           = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    inquiry_id: Mapped[str]   = mapped_column(String(36), index=True)
    stages_completed: Mapped[list] = mapped_column(JSON, default=list)
    current_stage: Mapped[str]    = mapped_column(String(50), default="inquiry")
    completed: Mapped[bool]       = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ── Convenience: log any action ───────────────────────────────────────────

async def log_action(
    session: AsyncSession,
    entity_type: str, entity_id: str,
    action: str, actor: str, details: dict,
) -> None:
    session.add(AuditLog(
        entity_type=entity_type, entity_id=entity_id,
        action=action, actor=actor, details=details,
    ))


# ── Demo: create all tables ───────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    async def main():
        init_db()
        await create_all_tables()
        print("All tables created:")
        for table in Base.metadata.sorted_tables:
            print(f"  ✓  {table.name}")

    asyncio.run(main())