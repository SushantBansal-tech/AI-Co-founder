# AI Sales Operations Agent

I have designed and implemented a local prototype of an AI-native B2B Sales Operations OS. It captures inquiries from website, email and WhatsApp, identifies the customer, retrieves their CRM history, resolves the product, checks feasibility, calculates pricing, generates quotations, follows up, handles negotiations, validates purchase orders and creates a final sales-order handoff.
The important architectural decision is that the LLM does not control money or authority. Gemini is used for language interpretation and communication, Qdrant stores narrative company knowledge, while PostgreSQL and deterministic services control pricing, inventory, credit, policies and approvals. Jarvis has a separate restricted identity and may only use registered business tools. Sensitive actions are stopped by a versioned authority engine and sent to the founder for approval.
## Overview

The system helps industrial businesses automate:

- Customer inquiry capture
- Requirement understanding
- Customer qualification
- Feasibility checks
- Pricing and quotation generation
- Follow-up management
- Negotiation support
- Purchase order validation
- Sales order handoff

Humans are involved only for high-risk approvals such as large discounts, low-margin quotations, credit exceptions, unusual payment terms, custom requirements, and purchase-order mismatches.

## High Level Architecture

![High Level Architecture](B2B%20Customer%20Sales%20Workflow-2026-08-13-193019.png)

## Core Sales Workflow

![Core Sales Workflow](B2B%20Customer%20Sales%20Workflow-2026-08-13-193125.png)

## How AI and Deterministic Logic Are Separated

![AI vs Deterministic Logic](B2B%20Customer%20Sales%20Workflow-2026-08-13-193155.png)




## Document Intelligence

The platform uses RAG for document-driven decisions.

Supported documents include:

- Product catalog
- Price list
- Discount policy
- Margin rules
- GST rates
- Customer CRM data
- Previous quotations
- Inventory reports
- Production capacity reports
- Raw-material costs
- Transport costs
- Payment terms
- Quotation templates
- Terms and conditions
- Purchase orders

Large documents are parsed, chunked, embedded, stored in Qdrant, and retrieved based on the current agent and task.

## Tech Stack

- Python
- FastAPI
- LangGraph
- Gemini 2.5 Flash
- Qdrant
- PostgreSQL / SQLite
- SQLAlchemy
- Pydantic
- RAG
- Docker

## Core Agents

- Inquiry Capture Agent
- Requirement Understanding Agent
- Customer Qualification Agent
- Feasibility Agent
- Pricing Agent
- Quotation Agent
- Follow-up Agent
- Negotiation Agent
- Purchase Order Agent
- Sales Order Handoff Agent

## Project Structure

```text
app/
├── agents/
├── graph/
├── documents/
└── main.py
```

## Run Locally

```bash
git clone https://github.com/SushantBansal-tech/Sales_Operation_OS.git
cd Sales_Operation_OS
python -m venv .venv
```

### Windows

```bash
.venv\Scripts\activate
```

### Linux/macOS

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the API:

```bash
uvicorn app.main:app --reload
```

Open Swagger UI:

```text
http://127.0.0.1:8000/docs
```

Upload company documents through `POST /documents/upload`. Use the canonical
agent names shown below in `allowed_agents_json`; older names such as
`requirement_agent` are accepted and normalized automatically.

```text
requirement_understanding, customer_qualification, internal_feasibility,
cost_and_pricing, quotation_generation, follow_up_management,
negotiation_support, purchase_order_handling, sales_order_handoff
```

Use `POST /rag/retrieve` to verify the exact chunks that a graph node will
receive. Every graph state must contain `business_id` so documents from
different businesses cannot be mixed. LangGraph nodes can be connected with
`with_rag_context(...)` from `app.rag.wrapper`; the wrapped handler receives
both `state` and `rag_context`.

## Future Scope

- WhatsApp and email integration
- ERP and CRM integration
- Inventory Agent
- Procurement Agent
- Production Planning Agent
- Finance Agent
- Dispatch Agent
- Company-wide AI Operations System

## Author

**Sushant Bansal**

GitHub: https://github.com/SushantBansal-tech
