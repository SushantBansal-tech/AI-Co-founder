import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args, get_origin

from sqlalchemy import delete, select, update

from app.database.models.structured import BusinessDocument, CustomerImportStaging
from app.database.models.customer import (
    OrderHistory, OrderStatus, PaymentRecord, QuotationHistory,
    QuotationStatus as HistoryQuotationStatus,
)
from app.structured_documents.readers import read_tabular_rows
from app.structured_documents.registry import IMPORT_REGISTRY, definition_for


def _date(value: str):
    if not value:
        return None
    for parser in (date.fromisoformat, lambda v: datetime.strptime(v, "%d-%m-%Y").date()):
        try:
            return parser(value)
        except ValueError:
            pass
    raise ValueError(f"invalid date '{value}'")


def _datetime(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.combine(_date(value), datetime.min.time())


def _convert(annotation, value: str):
    target = annotation
    args = get_args(target)
    if args:  # unwrap SQLAlchemy Mapped[T]
        target = args[0]
    optional_args = get_args(target)
    if optional_args:
        target = next(
            (arg for arg in optional_args if arg is not type(None)),
            target,
        )
    if value == "" and type(None) in optional_args:
        return None
    if target is Decimal:
        try:
            return Decimal(value.replace(",", "") or "0")
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal '{value}'") from exc
    if target is int:
        return int(Decimal(value.replace(",", "") or "0"))
    if target is bool:
        return value.lower() in {"1", "true", "yes", "active"}
    if target is date:
        return _date(value)
    if target is datetime:
        return _datetime(value)
    return value or None


class StructuredDocumentIngestionService:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def supports(filename: str) -> bool:
        return Path(filename).suffix.lower() in {".csv", ".xlsx"}

    async def ingest(self, *, path: str, filename: str, metadata, document_id: str) -> dict:
        definition = definition_for(filename)
        rows = read_tabular_rows(path)
        if not rows:
            raise ValueError("Structured document contains no data rows")
        missing = [column for column in definition.required if column not in rows[0]]
        if missing:
            raise ValueError(f"Missing required columns: {', '.join(missing)}")

        raw = Path(path).read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        model = definition.model
        errors = []
        converted = []
        external_customer_ids = []
        history_models = {OrderHistory, QuotationHistory, PaymentRecord}
        annotations = model.__annotations__
        for row in rows:
            values = {"business_id": metadata.business_id, "source_document_id": document_id}
            external_customer_ids.append(row.get("customer_id", ""))
            if model is CustomerImportStaging:
                row = {**row, "external_customer_id": row.get("customer_id", "")}
            for field, annotation in annotations.items():
                if field in {"id", "business_id", "source_document_id", "is_active", "created_at", "resolved_customer_id", "resolution_status"}:
                    continue
                if model in history_models and field == "customer_id":
                    continue
                if field in row:
                    try:
                        values[field] = _convert(annotation, row[field])
                    except ValueError as exc:
                        errors.append(f"row {row['_row_number']}, {field}: {exc}")
            converted.append(values)
        if errors:
            raise ValueError("Structured validation failed: " + "; ".join(errors[:20]))

        async with self.session_factory() as session:
            existing = await session.scalar(select(BusinessDocument).where(
                BusinessDocument.business_id == metadata.business_id,
                BusinessDocument.logical_name == filename.lower(),
                BusinessDocument.version == metadata.version,
            ))
            if existing:
                if existing.checksum_sha256 != checksum:
                    raise ValueError("The same document version already exists with different content; increment version")
                return {"status": "already_imported", "document_id": existing.id, "row_count": existing.row_count, "ingestion_mode": "structured"}

            previous_ids = list((await session.scalars(select(BusinessDocument.id).where(
                BusinessDocument.business_id == metadata.business_id,
                BusinessDocument.logical_name == filename.lower(),
                BusinessDocument.is_active.is_(True),
            ))).all())
            await session.execute(update(BusinessDocument).where(
                BusinessDocument.business_id == metadata.business_id,
                BusinessDocument.logical_name == filename.lower(),
                BusinessDocument.is_active.is_(True),
            ).values(is_active=False))
            if hasattr(model, "is_active"):
                await session.execute(update(model).where(
                    model.business_id == metadata.business_id,
                    model.is_active.is_(True),
                ).values(is_active=False))
            elif previous_ids:
                await session.execute(delete(model).where(
                    model.business_id == metadata.business_id,
                    model.source_document_id.in_(previous_ids),
                ))
            document = BusinessDocument(
                id=document_id, business_id=metadata.business_id,
                logical_name=filename.lower(), original_filename=filename,
                document_type=metadata.document_type.value, version=metadata.version,
                checksum_sha256=checksum, storage_path=path, import_status="processing",
            )
            session.add(document)
            await session.flush()
            if model in history_models:
                staging_rows = (await session.scalars(select(CustomerImportStaging).where(
                    CustomerImportStaging.business_id == metadata.business_id,
                    CustomerImportStaging.external_customer_id.in_(external_customer_ids),
                    CustomerImportStaging.resolved_customer_id.is_not(None),
                    CustomerImportStaging.is_active.is_(True),
                ))).all()
                customer_map = {
                    row.external_customer_id: row.resolved_customer_id
                    for row in staging_rows
                }
                unresolved = sorted(set(external_customer_ids) - set(customer_map))
                if unresolved:
                    raise ValueError(
                        "History rows reference unknown customer IDs: "
                        + ", ".join(unresolved[:20])
                        + ". Import customer_crm.csv first."
                    )
                for index, values in enumerate(converted):
                    values["customer_id"] = customer_map[external_customer_ids[index]]
                    if model is OrderHistory:
                        values["status"] = OrderStatus(str(values["status"]).lower())
                    elif model is QuotationHistory:
                        values["status"] = HistoryQuotationStatus(str(values["status"]).lower())
            session.add_all([model(**values) for values in converted])
            await session.flush()
            if model is CustomerImportStaging:
                await self._resolve_customer_rows(session, document_id, metadata.business_id)
            document.import_status = "completed"
            document.row_count = len(converted)
            document.completed_at = datetime.utcnow()
            await session.commit()

        return {"status": "imported", "document_id": document_id, "file_name": filename, "document_type": metadata.document_type.value, "row_count": len(converted), "ingestion_mode": "structured"}

    async def _resolve_customer_rows(self, session, document_id: str, business_id: str) -> None:
        from app.customers.identity_resolver import resolve_customer_identity
        from app.database.models.customer import PaymentBehavior

        rows = (await session.scalars(select(CustomerImportStaging).where(
            CustomerImportStaging.source_document_id == document_id
        ))).all()
        behavior_map = {
            "excellent": PaymentBehavior.EXCELLENT, "good": PaymentBehavior.GOOD,
            "average": PaymentBehavior.AVERAGE, "delayed": PaymentBehavior.POOR,
            "poor": PaymentBehavior.POOR,
        }
        for row in rows:
            resolution = await resolve_customer_identity(
                session, business_id=business_id, lead_id=None,
                company_name=row.company_name, contact_person=row.contact_person,
                email=row.email, phone=row.phone, gstin=row.gstin,
                sender_identifier=row.email or row.phone,
            )
            customer = resolution.customer
            customer.city = row.city or customer.city
            customer.state = row.state
            customer.customer_type = row.customer_type
            customer.credit_limit = row.credit_limit_inr
            customer.outstanding_amount = row.outstanding_amount_inr
            customer.payment_behavior = behavior_map.get(
                (row.payment_behavior or "").lower(), PaymentBehavior.UNKNOWN
            )
            customer.imported_previous_orders_count = row.previous_orders_count
            customer.imported_lifetime_sales = row.lifetime_sales_inr
            row.resolved_customer_id = customer.id
            row.resolution_status = resolution.resolution
