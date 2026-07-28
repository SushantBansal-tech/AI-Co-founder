from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import (
    BusinessEvent,
    Customer,
    CustomerIdentity,
    FollowUpRecord,
    Interaction,
    OrderHistory,
    PaymentRecord,
    PurchaseOrder,
    QuotationHistory,
    QuotationRecord,
    SalesOrder,
)


def _value(value):
    return value.value if hasattr(value, "value") else value


async def get_customer_360(
    session: AsyncSession,
    *,
    business_id: str,
    customer_id: str,
) -> dict:
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.business_id == business_id,
        )
    )
    if customer is None:
        raise ValueError("Customer not found.")

    identities = (
        await session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.business_id == business_id,
                CustomerIdentity.customer_id == customer_id,
            )
        )
    ).scalars().all()
    interactions = (
        await session.execute(
            select(Interaction)
            .where(
                Interaction.business_id == business_id,
                Interaction.customer_id == customer_id,
            )
            .order_by(Interaction.occurred_at.desc())
            .limit(20)
        )
    ).scalars().all()
    quotations = (
        await session.execute(
            select(QuotationRecord)
            .where(
                QuotationRecord.business_id == business_id,
                QuotationRecord.customer_id == customer_id,
            )
            .order_by(QuotationRecord.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    orders = (
        await session.execute(
            select(SalesOrder)
            .where(
                SalesOrder.business_id == business_id,
                SalesOrder.customer_id == customer_id,
            )
            .order_by(SalesOrder.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    payments = (
        await session.execute(
            select(PaymentRecord).where(
                PaymentRecord.business_id == business_id,
                PaymentRecord.customer_id == customer_id,
            )
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(BusinessEvent)
            .where(
                BusinessEvent.business_id == business_id,
                BusinessEvent.customer_id == customer_id,
            )
            .order_by(BusinessEvent.occurred_at.desc())
            .limit(30)
        )
    ).scalars().all()

    total_order_value = sum(float(order.total_value or 0) for order in orders)
    product_counts = Counter(
        order.product_description or order.product_code
        for order in orders
        if order.product_description or order.product_code
    )
    channel_counts = Counter(item.channel for item in interactions)
    delays = [payment.delay_days or 0 for payment in payments]
    history_counts = (
        await session.execute(
            select(
                func.count(QuotationHistory.id),
                func.count(QuotationHistory.id).filter(
                    QuotationHistory.status == "WON"
                ),
            ).where(
                QuotationHistory.business_id == business_id,
                QuotationHistory.customer_id == customer_id,
            )
        )
    ).one()
    history_total, history_won = history_counts
    quote_total = max(len(quotations), history_total or 0)
    win_rate = (float(history_won or 0) / quote_total * 100) if quote_total else 0

    return {
        "customer": {
            "id": customer.id,
            "business_id": customer.business_id,
            "company_name": customer.company_name,
            "contact_person": customer.contact_person,
            "email": customer.email,
            "phone": customer.phone,
            "gstin": customer.gstin,
            "city": customer.city,
            "status": customer.status,
            "credit_limit": float(customer.credit_limit or 0),
            "outstanding_amount": float(customer.outstanding_amount or 0),
            "payment_behavior": _value(customer.payment_behavior),
        },
        "identities": [
            {
                "type": item.identity_type,
                "value": item.raw_value,
                "normalized_value": item.normalized_value,
                "verified": item.is_verified,
            }
            for item in identities
        ],
        "summary": {
            "total_quotations": quote_total,
            "won_quotations": int(history_won or 0),
            "quotation_win_rate": round(win_rate, 2),
            "total_orders": len(orders),
            "total_order_value": round(total_order_value, 2),
            "average_order_value": round(
                total_order_value / len(orders), 2
            ) if orders else 0,
            "average_payment_delay_days": round(
                sum(delays) / len(delays), 2
            ) if delays else 0,
            "last_interaction_at": (
                interactions[0].occurred_at.isoformat()
                if interactions else None
            ),
            "last_order_at": (
                orders[0].created_at.isoformat() if orders else None
            ),
        },
        "preferences": {
            "products": [name for name, _ in product_counts.most_common(5)],
            "channel": (
                channel_counts.most_common(1)[0][0]
                if channel_counts else None
            ),
        },
        "recent_interactions": [
            {
                "id": item.id,
                "direction": item.direction,
                "channel": item.channel,
                "message_type": item.message_type,
                "content": item.content,
                "status": item.status,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in interactions
        ],
        "recent_quotations": [
            {
                "id": item.id,
                "quotation_number": item.quotation_number,
                "status": _value(item.status),
                "total_inc_gst": item.total_inc_gst,
                "created_at": item.created_at.isoformat(),
            }
            for item in quotations
        ],
        "recent_orders": [
            {
                "id": item.id,
                "po_number": item.po_number,
                "product_code": item.product_code,
                "product_description": item.product_description,
                "quantity": item.quantity,
                "total_value": item.total_value,
                "created_at": item.created_at.isoformat(),
            }
            for item in orders
        ],
        "recent_events": [
            {
                "event_type": item.event_type,
                "data": item.data,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in events
        ],
    }
