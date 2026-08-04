from dataclasses import dataclass

from app.database.models.structured import (
    CatalogProductRecord, CustomerImportStaging, DeliveryZoneRecord,
    DiscountBandRecord, GstRateRecord, InventoryRecord, MarginRuleRecord,
    PaymentTermRuleRecord, ProductCostRecord, ProductPriceRecord,
    ProductionCapacityRecord, TransportRateRecord,
)
from app.database.models.customer import OrderHistory, PaymentRecord, QuotationHistory


@dataclass(frozen=True)
class ImportDefinition:
    model: type
    required: tuple[str, ...]


IMPORT_REGISTRY = {
    "product_catalog.csv": ImportDefinition(CatalogProductRecord, ("product_code", "name", "category")),
    "product_catalog.xlsx": ImportDefinition(CatalogProductRecord, ("product_code", "name", "category")),
    "inventory_report.csv": ImportDefinition(InventoryRecord, ("product_code", "product_name", "warehouse", "available_qty")),
    "inventory_report.xlsx": ImportDefinition(InventoryRecord, ("product_code", "product_name", "warehouse", "available_qty")),
    "production_capacity.csv": ImportDefinition(ProductionCapacityRecord, ("product_code", "product_name", "plant", "available_daily_capacity", "estimated_lead_time_days")),
    "production_capacity.xlsx": ImportDefinition(ProductionCapacityRecord, ("product_code", "product_name", "plant", "available_daily_capacity", "estimated_lead_time_days")),
    "delivery_zones.csv": ImportDefinition(DeliveryZoneRecord, ("zone_code", "city", "transit_days")),
    "delivery_zones.xlsx": ImportDefinition(DeliveryZoneRecord, ("zone_code", "city", "transit_days")),
    "price_list.csv": ImportDefinition(ProductPriceRecord, ("product_code", "product_name", "base_price_inr")),
    "price_list.xlsx": ImportDefinition(ProductPriceRecord, ("product_code", "product_name", "base_price_inr")),
    "product_cost.csv": ImportDefinition(ProductCostRecord, ("product_code", "product_name", "rm_cost_per_mt", "manufacturing_overhead_pct")),
    "transport.csv": ImportDefinition(TransportRateRecord, ("transport_rate_id", "destination_city", "zone", "rate_per_mt_inr")),
    "discount_policy_normalized.csv": ImportDefinition(DiscountBandRecord, ("customer_type", "order_value_min", "order_value_max", "max_discount_pct", "approval_limit_pct")),
    "margin_rules.csv": ImportDefinition(MarginRuleRecord, ("rule_id", "minimum_margin_pct", "target_margin_pct")),
    "gst_rates.csv": ImportDefinition(GstRateRecord, ("gst_rule_id", "gst_rate_pct")),
    "payment_terms.csv": ImportDefinition(PaymentTermRuleRecord, ("term_id", "customer_type", "minimum_order_value_inr", "maximum_order_value_inr", "advance_percentage")),
    "customer_crm.csv": ImportDefinition(CustomerImportStaging, ("customer_id", "company_name")),
    "order_history.csv": ImportDefinition(OrderHistory, ("customer_id", "order_number", "product", "quantity", "order_value", "status", "order_date")),
    "previous_orders.csv": ImportDefinition(OrderHistory, ("customer_id", "order_number", "product", "quantity", "order_value", "status", "order_date")),
    "quotation_history.csv": ImportDefinition(QuotationHistory, ("customer_id", "quotation_number", "product", "quoted_value", "status", "quotation_date")),
    "previous_quotations.csv": ImportDefinition(QuotationHistory, ("customer_id", "quotation_number", "product", "quoted_value", "status", "quotation_date")),
    "payment_records.csv": ImportDefinition(PaymentRecord, ("customer_id", "invoice_number", "invoice_amount", "due_date")),
    "payment_history.csv": ImportDefinition(PaymentRecord, ("customer_id", "invoice_number", "invoice_amount", "due_date")),
}


def definition_for(filename: str) -> ImportDefinition:
    name = filename.strip().lower()
    definition = IMPORT_REGISTRY.get(name)
    if definition is None and name.endswith(".xlsx"):
        definition = IMPORT_REGISTRY.get(name[:-5] + ".csv")
    if definition is None:
        supported = ", ".join(sorted(IMPORT_REGISTRY))
        raise ValueError(f"No structured import mapping for '{filename}'. Supported names: {supported}")
    return definition
