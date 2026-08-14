# AI-native CRM application

The CRM reuses the existing sales database and LangGraph pipeline. It does not
copy customers, leads, quotations, orders, interactions, or pipeline state into
a second CRM database.

## Install the schema

```powershell
dotenv run -- alembic upgrade head
```

## Create the first administrator

```powershell
dotenv run -- python scripts/create_crm_admin.py `
  --business-id "demo-steel-company" `
  --email "admin@example.com" `
  --display-name "Sales Administrator"
```

The password is requested without echoing it in the terminal. Do not put a real
password on a shared command line.

## Start and open the application

```powershell
dotenv run -- uvicorn app.main:app --reload --loop asyncio
```

Open `http://127.0.0.1:8000/crm/app`.

## Security boundary

All `/crm/*` data APIs derive `business_id` from the authenticated membership.
The browser never selects the tenant after login. Existing channel ingestion
continues to use its independently authenticated source configuration and the
existing sales graph is unchanged for pricing, feasibility, quotation, PO, and
handoff decisions.

## Roles

- `admin`: tenant administration and all CRM actions
- `sales_manager`: customer, assignment, task, sales approval, and lost-deal actions
- `salesperson`: assigned customers/leads and assigned tasks
- `finance_manager`: finance approvals and CRM visibility
- `production_manager`: feasibility/PO approvals and CRM visibility
- `viewer`: read-only CRM visibility

## Important API conventions

- Login: `POST /crm/auth/login`
- Authenticated calls: `Authorization: Bearer <token>`
- Mutations: unique `Idempotency-Key` header
- Concurrent task updates: submit the current task `version`
- Tenant identity: always comes from the bearer session, never request JSON

Recommendation and reorder rule-engine features are intentionally not part of
this CRM release.
