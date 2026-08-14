# Batch 2: controlled business tools

Jarvis does not receive SQL, ORM sessions, Python execution, or generic update
functions. It authenticates as an AI service principal and can invoke only the
fixed tools registered in `app/business_tools/registry.py`.

## Request path

1. `X-AI-Principal-Token` authenticates Jarvis.
2. The fixed registry rejects unknown tools.
3. Pydantic validates the exact input model.
4. The executor checks the principal's required scope.
5. Mutations require `Idempotency-Key` and an authority decision.
6. The tenant-safe handler derives `business_id` from the principal.
7. Pydantic validates the output model.
8. Mutations write a business event and every call writes `ai_tool_executions`.

## Fixed catalog

Read-only tools:

- `search_customers`
- `get_customer_360`
- `get_lead`
- `get_pipeline`
- `get_pending_approvals`
- `get_inventory`
- `get_pricing_inputs`
- `get_open_tasks`

Controlled mutations:

- `add_customer_note`
- `create_task`
- `record_activity`
- `schedule_followup`
- `prepare_quotation`

`prepare_quotation` creates a deterministic `draft` quotation and version. It
never renders, dispatches, marks sent, or schedules a follow-up.

## API

List only tools visible to the current principal:

```http
GET /ai/tools
X-AI-Principal-Token: jarvis_live_...
```

Execute a read tool:

```http
POST /ai/tools/get_inventory/execute
X-AI-Principal-Token: jarvis_live_...
Content-Type: application/json

{"arguments":{"product_code":"MSB-001"}}
```

Execute a mutation:

```http
POST /ai/tools/add_customer_note/execute
X-AI-Principal-Token: jarvis_live_...
Idempotency-Key: a-unique-key-of-at-least-8-characters
Content-Type: application/json

{"arguments":{"customer_id":"...","content":"Customer prefers email."}}
```

Existing principals are deliberately not auto-granted new Batch 2 scopes. The
founder must grant the required scopes or create/rotate a specifically scoped
Jarvis identity through the CRM authority controls.
