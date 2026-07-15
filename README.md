# AI Sales Operations Agent

An agentic AI platform for automating the end-to-end B2B industrial sales workflow.

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

## Architecture

![AI Sales Operations Agent Architecture](Firebase%20Cloud%20Data-2026-07-15-180227.png)

The workflow is coordinated using LangGraph, with separate agents for each stage of the sales process.

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
