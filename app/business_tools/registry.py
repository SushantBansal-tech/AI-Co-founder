from fastapi import HTTPException

from app.business_tools.definitions import ToolDefinition
from app.business_tools.handlers import BusinessToolHandlers
from app.business_tools.schemas import (
    ActivityOutput,
    AddCustomerNoteInput,
    CreateTaskInput,
    Customer360Output,
    CustomerSalesContextInput,
    CustomerSalesContextOutput,
    CustomerIdInput,
    CustomerNoteOutput,
    InventoryInput,
    InventoryOutput,
    LeadIdInput,
    LeadOutput,
    OpenTasksInput,
    OpenTasksOutput,
    PendingApprovalsInput,
    PendingApprovalsOutput,
    PipelineInput,
    PipelineOutput,
    PrepareQuotationInput,
    PreparedQuotationOutput,
    PricingInputsInput,
    PricingInputsOutput,
    RecordActivityInput,
    ScheduleFollowupInput,
    ScheduleFollowupOutput,
    SearchCustomersInput,
    SearchCustomersOutput,
    TaskOutput,
)


class BusinessToolRegistry:
    def __init__(self, handlers: BusinessToolHandlers):
        definitions = (
            ToolDefinition("search_customers", SearchCustomersInput, SearchCustomersOutput,
                           "customer:read", "low", False, False, None,
                           handlers.search_customers),
            ToolDefinition("get_customer_360", CustomerIdInput, Customer360Output,
                           "customer:read", "low", False, False, None,
                           handlers.get_customer_360),
            ToolDefinition(
                "get_customer_sales_context",
                CustomerSalesContextInput,
                CustomerSalesContextOutput,
                "customer:read", "low", False, False, None,
                handlers.get_customer_sales_context,
            ),
            ToolDefinition("get_lead", LeadIdInput, LeadOutput,
                           "lead:read", "low", False, False, None,
                           handlers.get_lead),
            ToolDefinition("get_pipeline", PipelineInput, PipelineOutput,
                           "pipeline:read", "low", False, False, None,
                           handlers.get_pipeline),
            ToolDefinition("get_pending_approvals", PendingApprovalsInput,
                           PendingApprovalsOutput, "approval:read", "low",
                           False, False, None, handlers.get_pending_approvals),
            ToolDefinition("get_inventory", InventoryInput, InventoryOutput,
                           "inventory:read", "low", False, False, None,
                           handlers.get_inventory),
            ToolDefinition("get_pricing_inputs", PricingInputsInput,
                           PricingInputsOutput, "pricing_input:read", "low",
                           False, False, None, handlers.get_pricing_inputs),
            ToolDefinition("get_open_tasks", OpenTasksInput, OpenTasksOutput,
                           "task:read", "low", False, False, None,
                           handlers.get_open_tasks),
            ToolDefinition("add_customer_note", AddCustomerNoteInput,
                           CustomerNoteOutput, "customer_note:create", "low",
                           True, True, "ai_customer_note_added",
                           handlers.add_customer_note,
                           authority_action="add_customer_note",
                           entity_type="customer_note", entity_id_field="id"),
            ToolDefinition("create_task", CreateTaskInput, TaskOutput,
                           "task:create", "low", True, True,
                           "ai_crm_task_created", handlers.create_task,
                           authority_action="create_task",
                           entity_type="crm_task", entity_id_field="id"),
            ToolDefinition("record_activity", RecordActivityInput, ActivityOutput,
                           "activity:create", "low", True, True,
                           "ai_crm_activity_recorded", handlers.record_activity,
                           authority_action="record_activity",
                           entity_type="crm_activity", entity_id_field="id"),
            ToolDefinition("schedule_followup", ScheduleFollowupInput,
                           ScheduleFollowupOutput, "followup:schedule", "low",
                           True, True, "ai_followup_scheduled",
                           handlers.schedule_followup,
                           authority_action="schedule_followup",
                           entity_type="quotation", entity_id_field="quotation_id"),
            ToolDefinition("prepare_quotation", PrepareQuotationInput,
                           PreparedQuotationOutput, "quotation:prepare", "medium",
                           True, True, "ai_quotation_draft_prepared",
                           handlers.prepare_quotation,
                           authority_action="prepare_quotation",
                           permitted_authority_decisions=frozenset({
                               "allow", "prepare_only", "approval_required",
                           }),
                           entity_type="quotation", entity_id_field="quotation_id"),
        )
        self._definitions = {definition.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise RuntimeError("Controlled business tool names must be unique.")

    def require(self, name: str) -> ToolDefinition:
        definition = self._definitions.get(name)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"Unknown controlled tool: {name}")
        return definition

    def public_catalog(self) -> list[dict]:
        return [{
            "name": item.name,
            "required_scope": item.required_scope,
            "risk_level": item.risk_level,
            "requires_idempotency": item.requires_idempotency,
            "is_mutation": item.is_mutation,
            "input_schema": item.input_model.model_json_schema(),
            "output_schema": item.output_model.model_json_schema(),
        } for item in self._definitions.values()]
