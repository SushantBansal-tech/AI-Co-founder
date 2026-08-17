from app.database.models.activity import BusinessEvent, Interaction
from app.database.models.channel import (
    ChannelAttachment,
    ChannelConversation,
    ChannelCursor,
    ChannelInboundJob,
    ChannelIngestion,
    ChannelSource,
)
from app.database.models.idempotency import ProcessedEvent
from app.database.models.crm import (
    AuthSession,
    BusinessMembership,
    CRMActivity,
    CRMTask,
    LeadAssignment,
    User,
)
from app.database.models.authority import (
    AIPrincipalScope,
    AIServicePrincipal,
    AuthorityApprovalRequest,
    AuthorityDecision,
    AuthorityPolicy,
    AuthorityPolicyVersion,
    BusinessSettings,
    BusinessSettingVersion,
)
from app.database.models.business_tool import AIToolExecution
from app.database.models.ai_action import AIActionRequest, ApprovalDecision
from app.database.models.jarvis import JarvisConversation, JarvisMessage, JarvisRun
from app.database.models.memory import CustomerNote, MemoryOutbox
from app.database.models.pipeline import (
    InventoryReservation,
    PipelineInstance,
    QuotationDeliveryAttempt,
)
from app.database.models.customer import (
    Customer,
    CustomerIdentity,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
    OrderHistory,
    OrderStatus,
    PaymentBehavior,
    PaymentRecord,
    QuotationHistory,
    QuotationStatus as CustomerQuotationStatus,
)
from app.database.models.followup import (
    FollowUpRecord,
    FollowUpStatus,
    FollowUpType,
)
from app.database.models.followup_job import (
    FollowUpJob,
    FollowUpJobStatus,
)
from app.database.models.lead import (
    AuditLog,
    InquirySource,
    Lead,
    LeadStatus,
)
from app.database.models.quotation import (
    QuotationRecord,
    QuotationStatus,
    QuotationVersion,
)

from app.database.models.handoff import (
    HandoffRecord,
    HandoffRecordStatus,
)
from app.database.models.order import (
    POStatus,
    PurchaseOrder,
    SalesOrder,
)
from app.database.models.structured import (
    BusinessDocument, CatalogProductRecord, CustomerImportStaging,
    DeliveryZoneRecord, DiscountBandRecord, GstRateRecord, InventoryRecord,
    MarginRuleRecord, PaymentTermRuleRecord, ProductCostRecord,
    ProductPriceRecord, ProductionCapacityRecord, TransportRateRecord,
)

__all__ = [
    "Interaction",
    "BusinessEvent",
    "ProcessedEvent",
    "User",
    "BusinessMembership",
    "AuthSession",
    "LeadAssignment",
    "CRMTask",
    "CRMActivity",
    "BusinessSettings",
    "BusinessSettingVersion",
    "AIServicePrincipal",
    "AIPrincipalScope",
    "AuthorityDecision",
    "AuthorityApprovalRequest",
    "AuthorityPolicy",
    "AuthorityPolicyVersion",
    "AIToolExecution",
    "AIActionRequest",
    "ApprovalDecision",
    "JarvisConversation",
    "JarvisMessage",
    "JarvisRun",
    "CustomerNote",
    "MemoryOutbox",
    "PipelineInstance",
    "QuotationDeliveryAttempt",
    "InventoryReservation",
    "ChannelSource",
    "ChannelConversation",
    "ChannelIngestion",
    "ChannelCursor",
    "ChannelInboundJob",
    "ChannelAttachment",
    # Lead
    "InquirySource",
    "LeadStatus",
    "Lead",
    "AuditLog",

    # Customer
    "PaymentBehavior",
    "OrderStatus",
    "CustomerQuotationStatus",
    "Customer",
    "CustomerIdentity",
    "CustomerMatchReview",
    "CustomerMatchReviewStatus",
    "OrderHistory",
    "QuotationHistory",
    "PaymentRecord",

    # Quotation
    "QuotationStatus",
    "QuotationRecord",
    "QuotationVersion",

    # Follow-up
    "FollowUpStatus",
    "FollowUpType",
    "FollowUpRecord",
    "FollowUpJob",
    "FollowUpJobStatus",
    # Handoff
    "HandoffRecord",
    "HandoffRecordStatus",
    # Order
    "POStatus",
    "PurchaseOrder",
    "SalesOrder",
    "ChannelCursor",
    "ChannelInboundJob",
    "ChannelAttachment",
    "BusinessDocument", "CatalogProductRecord", "InventoryRecord",
    "ProductionCapacityRecord", "DeliveryZoneRecord", "ProductPriceRecord",
    "ProductCostRecord", "TransportRateRecord", "DiscountBandRecord",
    "MarginRuleRecord", "GstRateRecord", "PaymentTermRuleRecord",
    "CustomerImportStaging",
]
