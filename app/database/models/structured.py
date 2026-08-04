import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _id() -> str:
    return str(uuid.uuid4())


class BusinessDocument(Base):
    __tablename__ = "business_documents"
    __table_args__ = (UniqueConstraint("business_id", "logical_name", "version", name="uq_business_document_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    logical_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    document_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    import_status: Mapped[str] = mapped_column(String(30), default="processing", nullable=False, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class StructuredRowMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    business_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_document_id: Mapped[str] = mapped_column(String(36), ForeignKey("business_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CatalogProductRecord(StructuredRowMixin, Base):
    __tablename__ = "catalog_products"
    __table_args__ = (UniqueConstraint("source_document_id", "product_code", name="uq_catalog_document_product"),)
    product_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    grade: Mapped[Optional[str]] = mapped_column(String(120))
    specifications: Mapped[Optional[str]] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(30), default="MT", nullable=False)


class InventoryRecord(StructuredRowMixin, Base):
    __tablename__ = "inventory_records"
    product_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    warehouse: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    physical_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    reserved_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    available_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    damaged_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    stock_status: Mapped[Optional[str]] = mapped_column(String(50))
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ProductionCapacityRecord(StructuredRowMixin, Base):
    __tablename__ = "production_capacity_records"
    product_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    plant: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    daily_capacity: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    current_daily_load: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    available_daily_capacity: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    active_shifts: Mapped[int] = mapped_column(Integer, default=0)
    estimated_lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    capacity_status: Mapped[Optional[str]] = mapped_column(String(50))
    earliest_completion_date: Mapped[Optional[date]] = mapped_column(Date)


class DeliveryZoneRecord(StructuredRowMixin, Base):
    __tablename__ = "delivery_zone_records"
    zone_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    state: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    region: Mapped[Optional[str]] = mapped_column(String(120))
    pincode_start: Mapped[Optional[str]] = mapped_column(String(12))
    pincode_end: Mapped[Optional[str]] = mapped_column(String(12))
    transit_days: Mapped[int] = mapped_column(Integer, nullable=False)
    preferred_mode: Mapped[Optional[str]] = mapped_column(String(80))
    minimum_freight_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    service_level: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)


class ProductPriceRecord(StructuredRowMixin, Base):
    __tablename__ = "product_price_records"
    product_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="MT")
    base_price_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    minimum_order_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=0)
    freight_basis: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="active")


class ProductCostRecord(StructuredRowMixin, Base):
    __tablename__ = "product_cost_records"
    product_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    rm_cost_per_mt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    manufacturing_overhead_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)


class TransportRateRecord(StructuredRowMixin, Base):
    __tablename__ = "transport_rate_records"
    transport_rate_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    destination_city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    destination_state: Mapped[Optional[str]] = mapped_column(String(120))
    zone: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    distance_km: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    vehicle_type: Mapped[Optional[str]] = mapped_column(String(80))
    rate_per_mt_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    minimum_charge_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    handling_charge_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    estimated_transit_days: Mapped[int] = mapped_column(Integer, default=0)
    preferred_transporter: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="active")


class DiscountBandRecord(StructuredRowMixin, Base):
    __tablename__ = "discount_band_records"
    customer_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    order_value_min: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    order_value_max: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    max_discount_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    approval_limit_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)


class MarginRuleRecord(StructuredRowMixin, Base):
    __tablename__ = "margin_rule_records"
    rule_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    product_category: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    minimum_margin_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    target_margin_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    stretch_margin_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0)
    exception_approver: Mapped[Optional[str]] = mapped_column(String(120))
    exception_rule: Mapped[Optional[str]] = mapped_column(Text)
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="active")


class GstRateRecord(StructuredRowMixin, Base):
    __tablename__ = "gst_rate_records"
    gst_rule_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    product_category: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    hsn_code: Mapped[Optional[str]] = mapped_column(String(30))
    gst_rate_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    cgst_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0)
    sgst_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0)
    igst_pct: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0)
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="active")


class PaymentTermRuleRecord(StructuredRowMixin, Base):
    __tablename__ = "payment_term_rule_records"
    term_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    customer_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    minimum_order_value_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    maximum_order_value_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    advance_percentage: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    credit_days: Mapped[int] = mapped_column(Integer, default=0)
    balance_payment_condition: Mapped[Optional[str]] = mapped_column(Text)
    late_payment_interest_pct_pa: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=0)
    exception_approver: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="active")


class CustomerImportStaging(StructuredRowMixin, Base):
    __tablename__ = "customer_import_staging"
    external_customer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resolved_customer_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_person: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(120))
    state: Mapped[Optional[str]] = mapped_column(String(120))
    customer_type: Mapped[Optional[str]] = mapped_column(String(80))
    gstin: Mapped[Optional[str]] = mapped_column(String(30))
    credit_limit_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    outstanding_amount_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    payment_behavior: Mapped[Optional[str]] = mapped_column(String(50))
    previous_orders_count: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_sales_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    lead_source: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="active")
    resolution_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
