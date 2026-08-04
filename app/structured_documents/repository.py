from datetime import date

from sqlalchemy import func, or_, select

from app.database.models.structured import (
    CatalogProductRecord, DeliveryZoneRecord, DiscountBandRecord, GstRateRecord,
    InventoryRecord, MarginRuleRecord, PaymentTermRuleRecord, ProductCostRecord,
    ProductPriceRecord, ProductionCapacityRecord, TransportRateRecord,
)


class StructuredDataRepository:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def catalog_product(self, business_id: str, product_code: str):
        async with self.session_factory() as session:
            return await session.scalar(select(CatalogProductRecord).where(
                CatalogProductRecord.business_id == business_id,
                CatalogProductRecord.product_code == product_code,
                CatalogProductRecord.is_active.is_(True),
            ).order_by(CatalogProductRecord.created_at.desc()))

    async def feasibility_indexes(self, business_id: str):
        async with self.session_factory() as session:
            inventory = (await session.scalars(select(InventoryRecord).where(InventoryRecord.business_id == business_id, InventoryRecord.is_active.is_(True)))).all()
            capacity = (await session.scalars(select(ProductionCapacityRecord).where(ProductionCapacityRecord.business_id == business_id, ProductionCapacityRecord.is_active.is_(True)))).all()
            delivery = (await session.scalars(select(DeliveryZoneRecord).where(DeliveryZoneRecord.business_id == business_id, DeliveryZoneRecord.is_active.is_(True), func.lower(DeliveryZoneRecord.status) == "active"))).all()
        inv_index = {}
        for row in inventory:
            current = inv_index.setdefault(row.product_code, {"product_name": row.product_name, "available_qty": 0.0, "warehouses": [], "last_updated": ""})
            current["available_qty"] += float(row.available_qty)
            current["warehouses"].append(row.warehouse)
            if row.last_updated:
                current["last_updated"] = row.last_updated.isoformat()
        cap_index = {}
        for row in capacity:
            current = cap_index.setdefault(row.product_code, {"weekly_capacity_mt": 0.0, "lead_time_days": 0, "min_order_qty_mt": 0.0})
            current["weekly_capacity_mt"] += float(row.available_daily_capacity) * 7
            current["lead_time_days"] = max(current["lead_time_days"], row.estimated_lead_time_days)
        delivery_index = {
            row.city.strip().lower(): {
                "zone": row.region or row.zone_code,
                "transit_days": row.transit_days,
            }
            for row in delivery
        }
        return inv_index, cap_index, delivery_index

    async def pricing_documents(self, business_id: str):
        async with self.session_factory() as session:
            async def rows(model):
                return (await session.scalars(select(model).where(model.business_id == business_id, model.is_active.is_(True)))).all()
            return {
                "prices": await rows(ProductPriceRecord), "costs": await rows(ProductCostRecord),
                "transport": await rows(TransportRateRecord), "discounts": await rows(DiscountBandRecord),
                "margins": await rows(MarginRuleRecord), "gst": await rows(GstRateRecord),
            }

    async def payment_term(self, business_id: str, customer_type: str, order_value: float):
        async with self.session_factory() as session:
            return await session.scalar(select(PaymentTermRuleRecord).where(
                PaymentTermRuleRecord.business_id == business_id,
                PaymentTermRuleRecord.is_active.is_(True),
                func.lower(PaymentTermRuleRecord.status) == "active",
                func.lower(PaymentTermRuleRecord.customer_type) == customer_type.lower(),
                PaymentTermRuleRecord.minimum_order_value_inr <= order_value,
                PaymentTermRuleRecord.maximum_order_value_inr >= order_value,
            ).order_by(PaymentTermRuleRecord.minimum_order_value_inr.desc()))
