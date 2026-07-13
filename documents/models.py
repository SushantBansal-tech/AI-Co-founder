from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PRODUCT_CATALOG = "product_catalog"
    TECHNICAL_SPECIFICATION = "technical_specification"
    MANUFACTURING_CAPABILITY = "manufacturing_capability"

    CUSTOMER_HISTORY = "customer_history"
    PREVIOUS_ORDERS = "previous_orders"
    PREVIOUS_QUOTATIONS = "previous_quotations"
    PAYMENT_RECORDS = "payment_records"

    INVENTORY = "inventory"
    PRODUCTION_CAPACITY = "production_capacity"
    DELIVERY_POLICY = "delivery_policy"

    PRICING_SHEET = "pricing_sheet"
    DISCOUNT_POLICY = "discount_policy"
    TAX_POLICY = "tax_policy"
    MARGIN_POLICY = "margin_policy"

    QUOTATION_TEMPLATE = "quotation_template"
    PAYMENT_TERMS = "payment_terms"
    DELIVERY_TERMS = "delivery_terms"

    APPROVAL_MATRIX = "approval_matrix"
    PURCHASE_ORDER_POLICY = "purchase_order_policy"
    SALES_ORDER_POLICY = "sales_order_policy"


class DocumentUploadMetadata(BaseModel):
    business_id: str = Field(min_length=1, max_length=128)
    document_type: DocumentType
    allowed_agents: list[str]

    version: str = "1.0"
    status: str = "active"
    effective_from: Optional[str] = None


class ParsedDocument(BaseModel):
    document_id: str
    business_id: str
    file_name: str
    file_path: str
    document_type: DocumentType
    allowed_agents: list[str]
    text: str
    version: str
    status: str


class DocumentChunk(BaseModel):
    chunk_id: str
    document_id: str
    business_id: str

    document_name: str
    document_type: str
    allowed_agents: list[str]

    chunk_index: int
    chunk_text: str

    version: str
    status: str
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    document_type: str
    chunk_text: str
    similarity_score: float

    page_number: Optional[int] = None
    sheet_name: Optional[str] = None