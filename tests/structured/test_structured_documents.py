import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.database import CustomerImportStaging, OrderHistory
from sqlalchemy import select
from app.documents.models import DocumentType, DocumentUploadMetadata
from app.structured_documents import StructuredDataRepository, StructuredDocumentIngestionService


@pytest_asyncio.fixture
async def services(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'structured.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield StructuredDocumentIngestionService(sessions), StructuredDataRepository(sessions), tmp_path
    await engine.dispose()


async def _import(service, path: Path, business_id: str, version: str = "1.0"):
    return await service.ingest(
        path=str(path), filename=path.name,
        metadata=DocumentUploadMetadata(
            business_id=business_id, document_type=DocumentType.PRODUCT_CATALOG,
            allowed_agents=["requirement_understanding"], version=version,
        ),
        document_id=f"doc-{business_id}-{version}",
    )


@pytest.mark.asyncio
async def test_catalog_import_is_exact_and_tenant_isolated(services):
    service, repository, root = services
    source = root / "product_catalog.csv"
    source.write_text(
        "product_code,name,category,grade,specifications,unit\n"
        "MSB-001,Steel Billet,Steel Billet,IS2062,100x100mm,MT\n",
        encoding="utf-8",
    )
    result = await _import(service, source, "tenant-a")
    assert result["row_count"] == 1
    assert (await repository.catalog_product("tenant-a", "MSB-001")).name == "Steel Billet"
    assert await repository.catalog_product("tenant-b", "MSB-001") is None


@pytest.mark.asyncio
async def test_same_version_and_checksum_is_idempotent(services):
    service, _, root = services
    source = root / "product_catalog.csv"
    source.write_text(
        "product_code,name,category,grade,specifications,unit\n"
        "MSB-001,Steel Billet,Steel Billet,IS2062,100x100mm,MT\n",
        encoding="utf-8",
    )
    first = await _import(service, source, "tenant-a")
    second = await service.ingest(
        path=str(source), filename=source.name,
        metadata=DocumentUploadMetadata(
            business_id="tenant-a", document_type=DocumentType.PRODUCT_CATALOG,
            allowed_agents=["requirement_understanding"], version="1.0",
        ),
        document_id="unused-document-id",
    )
    assert first["document_id"] == second["document_id"]
    assert second["status"] == "already_imported"


@pytest.mark.asyncio
async def test_invalid_schema_does_not_create_import(services):
    service, repository, root = services
    source = root / "inventory_report.csv"
    source.write_text("wrong,column\nvalue,value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing required columns"):
        await service.ingest(
            path=str(source), filename=source.name,
            metadata=DocumentUploadMetadata(
                business_id="tenant-a", document_type=DocumentType.INVENTORY,
                allowed_agents=["internal_feasibility"], version="1.0",
            ),
            document_id="bad-document",
        )
    inventory, _, _ = await repository.feasibility_indexes("tenant-a")
    assert inventory == {}


@pytest.mark.asyncio
async def test_customer_and_history_import_resolve_permanent_customer(services):
    service, _, root = services
    crm = root / "customer_crm.csv"
    crm.write_text(
        "customer_id,company_name,contact_person,phone,email,city,state,customer_type,gstin,credit_limit_inr,outstanding_amount_inr,payment_behavior,previous_orders_count,lifetime_sales_inr,lead_source,status\n"
        "CUST-0001,ABC Steel,Ravi,+919876543210,ravi@example.com,Mumbai,Maharashtra,Dealer,27ABCDE1234F1Z5,1000000,100000,Good,3,5000000,Website,Active\n",
        encoding="utf-8",
    )
    metadata = DocumentUploadMetadata(
        business_id="tenant-a", document_type=DocumentType.CUSTOMER_HISTORY,
        allowed_agents=["customer_qualification"], version="1.0",
    )
    await service.ingest(
        path=str(crm), filename=crm.name, metadata=metadata,
        document_id="crm-document",
    )
    history = root / "order_history.csv"
    history.write_text(
        "customer_id,order_number,product,quantity,order_value,status,order_date\n"
        "CUST-0001,SO-001,Steel Billet,100 MT,9000000,delivered,2026-06-01\n",
        encoding="utf-8",
    )
    await service.ingest(
        path=str(history), filename=history.name,
        metadata=metadata.model_copy(update={"document_type": DocumentType.PREVIOUS_ORDERS}),
        document_id="order-history-document",
    )
    async with service.session_factory() as session:
        staging = await session.scalar(select(CustomerImportStaging))
        order = await session.scalar(select(OrderHistory))
        assert staging.resolved_customer_id == order.customer_id
        assert order.order_number == "SO-001"
