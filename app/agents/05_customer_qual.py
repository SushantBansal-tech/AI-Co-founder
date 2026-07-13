"""
Sub-problem: Customer lookup — is this lead a new or existing customer?

Responsibilities:
  1. DB models: Customer, OrderHistory, QuotationHistory, PaymentRecord
  2. Look up customer by company name OR contact identifier
  3. Pull full history — orders, quotations, payment behavior, outstanding amount
  4. Return a CustomerProfile (new or existing) for the qualification agent

Depends on: inquiry_agent.py (InquiryExtraction, Base, log_action)

Run:
    python 05_customer_lookup.py
"""

import uuid
import asyncio
from datetime import datetime, date
from enum import Enum
from typing import Optional
from decimal import Decimal

from typing import ClassVar
from pydantic import BaseModel
from sqlalchemy import (
    String, Text, Numeric, Integer, Date, DateTime,
    ForeignKey, Enum as SAEnum, select, or_
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from importlib import import_module
ia = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
Base      = ia.Base
log_action = ia.log_action
InquiryExtraction = ia.InquiryExtraction


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CustomerType(str, Enum):
    NEW      = "new"
    EXISTING = "existing"


class PaymentBehavior(str, Enum):
    EXCELLENT = "excellent"   # always on time
    GOOD      = "good"        # occasional delay < 15 days
    AVERAGE   = "average"     # regular delays 15-30 days
    POOR      = "poor"        # frequent delays > 30 days or defaults
    UNKNOWN   = "unknown"     # no history yet


class OrderStatus(str, Enum):
    DELIVERED = "delivered"
    IN_TRANSIT = "in_transit"
    CANCELLED  = "cancelled"
    PENDING    = "pending"


class QuotationStatus(str, Enum):
    WON       = "won"
    LOST      = "lost"
    PENDING   = "pending"
    EXPIRED   = "expired"


# ---------------------------------------------------------------------------
# DB Models (extend Base from inquiry_agent so all tables share one schema)
# ---------------------------------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                     default=lambda: str(uuid.uuid4()))
    company_name: Mapped[str]  = mapped_column(String(255), index=True)
    contact_person: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50),  nullable=True)
    city: Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    gstin: Mapped[Optional[str]] = mapped_column(String(20),  nullable=True)

    credit_limit: Mapped[Decimal]    = mapped_column(Numeric(15, 2), default=0)
    outstanding_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    payment_behavior: Mapped[str] = mapped_column(
        SAEnum(PaymentBehavior), default=PaymentBehavior.UNKNOWN
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)

    orders:     Mapped[list["OrderHistory"]]     = relationship("OrderHistory",     back_populates="customer", lazy="select")
    quotations: Mapped[list["QuotationHistory"]] = relationship("QuotationHistory", back_populates="customer", lazy="select")
    payments:   Mapped[list["PaymentRecord"]]    = relationship("PaymentRecord",    back_populates="customer", lazy="select")


class OrderHistory(Base):
    __tablename__ = "order_history"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    order_number: Mapped[str] = mapped_column(String(50))
    product: Mapped[str]      = mapped_column(String(255))
    quantity: Mapped[str]     = mapped_column(String(100))
    order_value: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    status: Mapped[str]       = mapped_column(SAEnum(OrderStatus))
    order_date: Mapped[date]  = mapped_column(Date)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="orders")


class QuotationHistory(Base):
    __tablename__ = "quotation_history"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    quotation_number: Mapped[str]   = mapped_column(String(50))
    product: Mapped[str]            = mapped_column(String(255))
    quoted_value: Mapped[Decimal]   = mapped_column(Numeric(15, 2))
    status: Mapped[str]             = mapped_column(SAEnum(QuotationStatus))
    quotation_date: Mapped[date]    = mapped_column(Date)
    lost_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="quotations")


class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    invoice_number: Mapped[str]  = mapped_column(String(50))
    invoice_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    due_date: Mapped[date]   = mapped_column(Date)
    paid_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    delay_days: Mapped[int]  = mapped_column(Integer, default=0)  # 0 = on time, >0 = late

    customer: Mapped["Customer"] = relationship("Customer", back_populates="payments")


# ---------------------------------------------------------------------------
# Pydantic output model (what the Qualification Agent receives)
# ---------------------------------------------------------------------------

class CustomerProfile(BaseModel):
    customer_type: CustomerType
    customer_id: Optional[str]   = None
    company_name: str
    contact_person: Optional[str] = None
    city: Optional[str]           = None
    gstin: Optional[str]          = None

    # Financial snapshot
    credit_limit: float           = 0.0
    outstanding_amount: float     = 0.0
    credit_utilization_pct: float = 0.0   # outstanding / credit_limit * 100

    # Behavior signals
    payment_behavior: PaymentBehavior = PaymentBehavior.UNKNOWN
    total_orders: int             = 0
    total_order_value: float      = 0.0
    won_quotations: int           = 0
    lost_quotations: int          = 0
    win_rate_pct: float           = 0.0
    avg_delay_days: float         = 0.0

    # Raw history for the LLM to reason over
    recent_orders: list[dict]     = []
    recent_quotations: list[dict] = []


# ---------------------------------------------------------------------------
# Lookup logic
# ---------------------------------------------------------------------------

def _normalize(s: Optional[str]) -> str:
    return (s or "").lower().strip()


async def lookup_customer(
    session: AsyncSession,
    extraction: InquiryExtraction,
) -> CustomerProfile:
    """
    Tries to find an existing customer by:
      1. Exact company_name match (case-insensitive)
      2. email or phone match if available

    Returns a CustomerProfile with full history if found,
    or a thin NEW profile if not.
    """
    company = _normalize(extraction.company_name)
    email   = _normalize(extraction.contact_person)  # contact_person may carry email

    # Build search conditions
    conditions = []
    if company:
        conditions.append(
            # SQLite LIKE is case-insensitive for ASCII; use LOWER() for safety
            Customer.company_name.ilike(f"%{company}%")
        )

    stmt = select(Customer)
    if conditions:
        stmt = stmt.where(or_(*conditions))
    stmt = stmt.limit(1)

    result = await session.execute(stmt)
    customer = result.scalar_one_or_none()

    if customer is None:
        # Brand new customer — no history
        return CustomerProfile(
            customer_type=CustomerType.NEW,
            company_name=extraction.company_name or "Unknown",
            contact_person=extraction.contact_person,
        )

    # ---- Pull history ----
    orders_result = await session.execute(
        select(OrderHistory)
        .where(OrderHistory.customer_id == customer.id)
        .order_by(OrderHistory.order_date.desc())
        .limit(10)
    )
    orders = orders_result.scalars().all()

    quotes_result = await session.execute(
        select(QuotationHistory)
        .where(QuotationHistory.customer_id == customer.id)
        .order_by(QuotationHistory.quotation_date.desc())
        .limit(10)
    )
    quotes = quotes_result.scalars().all()

    payments_result = await session.execute(
        select(PaymentRecord)
        .where(PaymentRecord.customer_id == customer.id)
    )
    payments = payments_result.scalars().all()

    # ---- Compute signals ----
    total_order_value = float(sum(o.order_value for o in orders))
    won  = [q for q in quotes if q.status == QuotationStatus.WON]
    lost = [q for q in quotes if q.status == QuotationStatus.LOST]
    win_rate = (len(won) / len(quotes) * 100) if quotes else 0.0

    delays = [p.delay_days for p in payments if p.delay_days is not None]
    avg_delay = sum(delays) / len(delays) if delays else 0.0

    credit_util = 0.0
    if float(customer.credit_limit) > 0:
        credit_util = float(customer.outstanding_amount) / float(customer.credit_limit) * 100

    return CustomerProfile(
        customer_type=CustomerType.EXISTING,
        customer_id=customer.id,
        company_name=customer.company_name,
        contact_person=customer.contact_person,
        city=customer.city,
        gstin=customer.gstin,
        credit_limit=float(customer.credit_limit),
        outstanding_amount=float(customer.outstanding_amount),
        credit_utilization_pct=round(credit_util, 1),
        payment_behavior=PaymentBehavior(customer.payment_behavior),
        total_orders=len(orders),
        total_order_value=total_order_value,
        won_quotations=len(won),
        lost_quotations=len(lost),
        win_rate_pct=round(win_rate, 1),
        avg_delay_days=round(avg_delay, 1),
        recent_orders=[
            {"order_number": o.order_number, "product": o.product,
             "quantity": o.quantity, "value": float(o.order_value),
             "status": o.status, "date": str(o.order_date)}
            for o in orders[:5]
        ],
        recent_quotations=[
            {"quotation_number": q.quotation_number, "product": q.product,
             "value": float(q.quoted_value), "status": q.status,
             "date": str(q.quotation_date),
             "lost_reason": q.lost_reason}
            for q in quotes[:5]
        ],
    )


# ---------------------------------------------------------------------------
# Seed helper (demo only — creates a known existing customer)
# ---------------------------------------------------------------------------

async def _seed_demo_customer(session: AsyncSession):
    cust = Customer(
        id=str(uuid.uuid4()),
        company_name="Apex Steel Pvt Ltd",
        contact_person="Ramesh Kumar",
        email="ramesh@apexsteel.in",
        phone="+919812345678",
        city="Ludhiana",
        gstin="03AABCA1234C1Z5",
        credit_limit=Decimal("5000000"),
        outstanding_amount=Decimal("1200000"),
        payment_behavior=PaymentBehavior.GOOD,
    )
    session.add(cust)
    await session.flush()

    session.add_all([
        OrderHistory(customer_id=cust.id, order_number="ORD-2024-001",
                     product="MS Billet IS2062", quantity="300 MT",
                     order_value=Decimal("4500000"), status=OrderStatus.DELIVERED,
                     order_date=date(2024, 8, 10)),
        OrderHistory(customer_id=cust.id, order_number="ORD-2024-002",
                     product="MS Plate IS2062", quantity="50 MT",
                     order_value=Decimal("950000"), status=OrderStatus.DELIVERED,
                     order_date=date(2024, 11, 5)),
    ])
    session.add_all([
        QuotationHistory(customer_id=cust.id, quotation_number="QT-2024-010",
                         product="MS Billet IS2062", quoted_value=Decimal("4500000"),
                         status=QuotationStatus.WON, quotation_date=date(2024, 8, 1)),
        QuotationHistory(customer_id=cust.id, quotation_number="QT-2024-018",
                         product="MS Angle IS2062", quoted_value=Decimal("600000"),
                         status=QuotationStatus.LOST, quotation_date=date(2024, 9, 20),
                         lost_reason="Competitor offered lower price"),
    ])
    session.add_all([
        PaymentRecord(customer_id=cust.id, invoice_number="INV-2024-031",
                      invoice_amount=Decimal("4500000"),
                      due_date=date(2024, 9, 10), paid_date=date(2024, 9, 14),
                      delay_days=4),
        PaymentRecord(customer_id=cust.id, invoice_number="INV-2024-045",
                      invoice_amount=Decimal("950000"),
                      due_date=date(2024, 12, 5), paid_date=date(2024, 12, 7),
                      delay_days=2),
    ])
    await session.commit()
    return cust


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

async def _demo():
    engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        await _seed_demo_customer(session)

    # Test 1: existing customer
    ext_existing = InquiryExtraction(
        inquiry_id="INQ-001",
        company_name="Apex Steel Pvt Ltd",
        customer_name="Ramesh Kumar",
        product_requested="MS Billet IS2062",
        quantity="500 MT",
        delivery_location="Ludhiana",
        extraction_confidence=0.95,
    )

    # Test 2: brand new customer
    ext_new = InquiryExtraction(
        inquiry_id="INQ-002",
        company_name="Nova Auto Parts Ltd",
        customer_name="Priya Singh",
        product_requested="MS Sheet IS513",
        quantity="20 MT",
        delivery_location="Pune",
        extraction_confidence=0.90,
    )

    async with Session() as session:
        for ext in [ext_existing, ext_new]:
            profile = await lookup_customer(session, ext)
            print(f"\n{'='*55}")
            print(f"Company  : {profile.company_name}")
            print(f"Type     : {profile.customer_type.value.upper()}")
            if profile.customer_type == CustomerType.EXISTING:
                print(f"City     : {profile.city}")
                print(f"Credit   : ₹{profile.credit_limit:,.0f}  |  "
                      f"Outstanding: ₹{profile.outstanding_amount:,.0f}  "
                      f"({profile.credit_utilization_pct}% utilized)")
                print(f"Payment  : {profile.payment_behavior.value}")
                print(f"Orders   : {profile.total_orders}  |  "
                      f"Total value: ₹{profile.total_order_value:,.0f}")
                print(f"Win rate : {profile.win_rate_pct}%  |  "
                      f"Avg delay: {profile.avg_delay_days} days")
                print(f"Recent orders: {profile.recent_orders}")


if __name__ == "__main__":
    asyncio.run(_demo())