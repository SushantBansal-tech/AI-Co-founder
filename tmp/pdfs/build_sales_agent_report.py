from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


OUTPUT = Path("output/pdf/ai_sales_agent_capability_report.pdf")

NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#2563EB")
TEAL = colors.HexColor("#0F766E")
LIGHT_BLUE = colors.HexColor("#EAF2FF")
LIGHT_TEAL = colors.HexColor("#E8F6F3")
LIGHT_GREY = colors.HexColor("#F3F5F7")
MID_GREY = colors.HexColor("#D5DBE3")
DARK_GREY = colors.HexColor("#384454")
AMBER = colors.HexColor("#B45309")
LIGHT_AMBER = colors.HexColor("#FFF4E5")
GREEN = colors.HexColor("#15803D")
WHITE = colors.white


styles = getSampleStyleSheet()
styles.add(
    ParagraphStyle(
        name="ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=27,
        leading=32,
        textColor=NAVY,
        alignment=TA_LEFT,
        spaceAfter=8,
    )
)
styles.add(
    ParagraphStyle(
        name="Subtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=12,
        leading=18,
        textColor=DARK_GREY,
        spaceAfter=14,
    )
)
styles.add(
    ParagraphStyle(
        name="SectionTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=19,
        leading=23,
        textColor=NAVY,
        spaceBefore=2,
        spaceAfter=11,
    )
)
styles.add(
    ParagraphStyle(
        name="Subsection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=TEAL,
        spaceBefore=8,
        spaceAfter=5,
    )
)
styles.add(
    ParagraphStyle(
        name="BodySmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=13.3,
        textColor=DARK_GREY,
        spaceAfter=6,
    )
)
styles.add(
    ParagraphStyle(
        name="Body",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14.5,
        textColor=DARK_GREY,
        spaceAfter=7,
    )
)
styles.add(
    ParagraphStyle(
        name="CardTitle",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=NAVY,
        spaceAfter=4,
    )
)
styles.add(
    ParagraphStyle(
        name="CardBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.7,
        leading=12,
        textColor=DARK_GREY,
    )
)
styles.add(
    ParagraphStyle(
        name="TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=10.5,
        textColor=WHITE,
        alignment=TA_LEFT,
    )
)
styles.add(
    ParagraphStyle(
        name="TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.2,
        leading=10.7,
        textColor=DARK_GREY,
    )
)
styles.add(
    ParagraphStyle(
        name="TableCellBold",
        parent=styles["TableCell"],
        fontName="Helvetica-Bold",
        textColor=NAVY,
    )
)
styles.add(
    ParagraphStyle(
        name="Callout",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=NAVY,
        alignment=TA_CENTER,
    )
)
styles.add(
    ParagraphStyle(
        name="Stage",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.2,
        leading=10,
        alignment=TA_CENTER,
        textColor=NAVY,
    )
)


def P(text, style="Body"):
    return Paragraph(text, styles[style])


def bullet(text):
    return Paragraph(
        f"<font color='#2563EB'>-</font> {text}",
        styles["BodySmall"],
    )


def table(data, widths, header=True, font_size=8.2):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, MID_GREY),
    ]
    if header:
        commands += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("LINEBELOW", (0, 0), (-1, 0), 0, NAVY),
        ]
        start = 1
    else:
        start = 0
    for row in range(start, len(data)):
        if row % 2 == start % 2:
            commands.append(("BACKGROUND", (0, row), (-1, row), LIGHT_GREY))
    t.setStyle(TableStyle(commands))
    return t


def info_card(title, body, color=LIGHT_BLUE):
    card = Table(
        [[P(title, "CardTitle")], [P(body, "CardBody")]],
        colWidths=[78 * mm],
    )
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), color),
                ("BOX", (0, 0), (-1, -1), 0.6, MID_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return card


def header_footer(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 13 * mm, width, 13 * mm, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.drawString(18 * mm, height - 8.5 * mm, "AI SALES OPERATIONS AGENT")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(
        width - 18 * mm,
        height - 8.5 * mm,
        "Capability Report | 24 July 2026",
    )
    canvas.setStrokeColor(MID_GREY)
    canvas.line(18 * mm, 13 * mm, width - 18 * mm, 13 * mm)
    canvas.setFillColor(DARK_GREY)
    canvas.setFont("Helvetica", 7.8)
    canvas.drawString(18 * mm, 8 * mm, "Local validation environment - demo-steel-company")
    canvas.drawRightString(width - 18 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


doc = SimpleDocTemplate(
    str(OUTPUT),
    pagesize=A4,
    leftMargin=18 * mm,
    rightMargin=18 * mm,
    topMargin=21 * mm,
    bottomMargin=18 * mm,
    title="AI Sales Agent Capability Report",
    author="Codex",
    subject="Business and technical capability assessment",
)

story = []

# Cover
story += [
    Spacer(1, 18 * mm),
    P("AI Sales Operations Agent", "ReportTitle"),
    P(
        "Current business capabilities, technical architecture, validated workflows, "
        "controls and production-readiness assessment",
        "Subtitle",
    ),
    Spacer(1, 4 * mm),
]

cover_band = Table(
    [
        [
            P("BUSINESS SCOPE", "TableHeader"),
            P("TECHNICAL FOUNDATION", "TableHeader"),
            P("VALIDATED OUTCOME", "TableHeader"),
        ],
        [
            P(
                "Inquiry capture through quotation, follow-up, negotiation, PO validation, "
                "sales order and department handoff.",
                "TableCell",
            ),
            P(
                "FastAPI, LangGraph, Gemini, local Qdrant, local embeddings, SQLite, "
                "agent-scoped retrieval and checkpoints.",
                "TableCell",
            ),
            P(
                "A live MSB-076 scenario reached handed_off with production, inventory, "
                "purchase, dispatch and finance notified.",
                "TableCell",
            ),
        ],
    ],
    colWidths=[55 * mm, 55 * mm, 55 * mm],
)
cover_band.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("BACKGROUND", (0, 1), (-1, 1), LIGHT_BLUE),
            ("BOX", (0, 0), (-1, -1), 0.8, NAVY),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]
    )
)
story += [cover_band, Spacer(1, 12 * mm)]

summary_box = Table(
    [
        [P("Executive position", "CardTitle")],
        [
            P(
                "The system is a functional local sales-operations orchestrator for an "
                "industrial B2B workflow. It combines LLM reasoning for language tasks "
                "with deterministic calculations and exact structured-document retrieval "
                "for inventory, capacity, delivery, pricing, margin, tax, transport and "
                "discount decisions. It has been validated from inquiry through final "
                "sales-order handoff in the local test environment.",
                "Body",
            )
        ],
    ],
    colWidths=[165 * mm],
)
summary_box.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_TEAL),
            ("BOX", (0, 0), (-1, -1), 0.8, TEAL),
            ("LEFTPADDING", (0, 0), (-1, -1), 11),
            ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]
    )
)
story += [
    summary_box,
    Spacer(1, 12 * mm),
    P("Report basis", "Subsection"),
    bullet("Repository implementation and uploaded demo-steel-company document pack."),
    bullet("Live API validation performed on 24 July 2026."),
    bullet("Local test data includes synthetic product-cost and normalized discount records."),
    Spacer(1, 9 * mm),
    P("Current maturity", "Subsection"),
]
maturity = Table(
    [
        [P("Functional prototype", "Callout"), P("Controlled local pilot", "Callout"), P("Production hardening required", "Callout")],
        [P("Completed", "TableCell"), P("Suitable with supervised users", "TableCell"), P("Before unsupervised customer use", "TableCell")],
    ],
    colWidths=[55 * mm] * 3,
)
maturity.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (0, -1), LIGHT_TEAL),
            ("BACKGROUND", (1, 0), (1, -1), LIGHT_BLUE),
            ("BACKGROUND", (2, 0), (2, -1), LIGHT_AMBER),
            ("BOX", (0, 0), (-1, -1), 0.6, MID_GREY),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
    )
)
story += [maturity, PageBreak()]

# Business capabilities
story += [
    P("1. Business capabilities", "SectionTitle"),
    P(
        "The agent covers the main commercial lifecycle of an industrial B2B sales inquiry. "
        "It is designed to assist sales operations, not replace authorized human decisions.",
        "Body",
    ),
]

business_rows = [
    [P("Capability", "TableHeader"), P("What the agent does", "TableHeader"), P("Business value", "TableHeader")],
    [P("Inquiry capture", "TableCellBold"), P("Normalizes email or message text, extracts customer, company, product, quantity, specifications, delivery and payment expectations, and requests missing information.", "TableCell"), P("Reduces manual data entry and incomplete leads.", "TableCell")],
    [P("Requirement matching", "TableCellBold"), P("Matches a request to catalog evidence, classifies exact, near or custom, identifies specification gaps and flags technical review.", "TableCell"), P("Prevents unsupported products from being quoted as standard items.", "TableCell")],
    [P("Customer qualification", "TableCellBold"), P("Looks up customer history, payment behavior, credit position, order history and lead quality, then assigns score, temperature and priority.", "TableCell"), P("Helps sales teams prioritize and identify credit risk.", "TableCell")],
    [P("Feasibility", "TableCellBold"), P("Checks stock, available production capacity, delivery zone, transit time, lead time and deadline risk.", "TableCell"), P("Avoids commitments that operations cannot fulfill.", "TableCell")],
    [P("Pricing", "TableCellBold"), P("Calculates RM cost, overhead, transport, floor price, list price, discount, margin, GST and total invoice value.", "TableCell"), P("Creates auditable prices and blocks incomplete pricing data.", "TableCell")],
    [P("Quotation", "TableCellBold"), P("Builds a structured quotation draft, renders HTML, assigns payment terms, delivery timeline, validity and approval reasons, and saves the record.", "TableCell"), P("Standardizes quote quality and commercial terms.", "TableCell")],
    [P("Follow-up", "TableCellBold"), P("Generates scheduled reminder messages with escalating tone and logs follow-up attempts.", "TableCell"), P("Improves response discipline and pipeline coverage.", "TableCell")],
    [P("Negotiation", "TableCellBold"), P("Classifies replies, detects objections and counteroffers, compares offers with floor price, revises quotations or rejects unsafe offers.", "TableCell"), P("Protects margin while accelerating negotiation response.", "TableCell")],
    [P("PO validation", "TableCellBold"), P("Extracts PO fields and compares product, quantity, rate, GST, total, payment and delivery terms with the latest quotation.", "TableCell"), P("Reduces order-entry errors and commercial leakage.", "TableCell")],
    [P("Sales order and handoff", "TableCellBold"), P("Marks the lead won, creates a sales order, prepares department packages and notifies production, inventory, purchase, dispatch and finance.", "TableCell"), P("Provides a controlled transition from sales to execution.", "TableCell")],
]
story += [table(business_rows, [37 * mm, 84 * mm, 44 * mm]), PageBreak()]

# Lifecycle
story += [
    P("2. End-to-end operating model", "SectionTitle"),
    P(
        "A stable thread ID keeps one lead's state across separate customer and internal "
        "events. The graph is re-entered with a trigger rather than rerunning the entire "
        "sales lifecycle from the beginning.",
        "Body",
    ),
]

stages = [
    "Inquiry",
    "Requirement",
    "Qualification",
    "Feasibility",
    "Pricing",
    "Quotation",
    "Follow-up",
    "Reply / negotiation",
    "PO validation",
    "Sales order",
    "Handoff",
]
stage_rows = []
for start in range(0, len(stages), 4):
    subset = stages[start : start + 4]
    cells = [P(stage, "Stage") for stage in subset]
    while len(cells) < 4:
        cells.append("")
    stage_rows.append(cells)
stage_table = Table(stage_rows, colWidths=[40 * mm] * 4, rowHeights=[17 * mm] * len(stage_rows))
stage_table.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
            ("BOX", (0, 0), (-1, -1), 0.7, BLUE),
            ("INNERGRID", (0, 0), (-1, -1), 0.7, WHITE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]
    )
)
story += [stage_table, Spacer(1, 8 * mm)]

story += [
    P("Event triggers", "Subsection"),
    table(
        [
            [P("Trigger", "TableHeader"), P("Pipeline entered", "TableHeader"), P("Typical final state", "TableHeader")],
            [P("inquiry", "TableCellBold"), P("Capture through quotation", "TableCell"), P("quotation_sent or a controlled gate", "TableCell")],
            [P("followup", "TableCellBold"), P("Reminder composition and dispatch", "TableCell"), P("followup_sent", "TableCell")],
            [P("customer_reply", "TableCellBold"), P("Reply analysis, objection or counteroffer", "TableCell"), P("message dispatched, revised quote or approval", "TableCell")],
            [P("po_received", "TableCellBold"), P("PO extraction, validation and order handoff", "TableCell"), P("handed_off, correction request or PO approval", "TableCell")],
            [P("approved", "TableCellBold"), P("Resume the exact paused stage", "TableCell"), P("Next stage or final outcome", "TableCell")],
        ],
        [34 * mm, 76 * mm, 55 * mm],
    ),
    Spacer(1, 8 * mm),
    P("Human control points", "Subsection"),
]

approval_cards = Table(
    [
        [
            info_card("Qualification", "Credit-risk exceptions and uncertain customer exposure.", LIGHT_AMBER),
            info_card("Feasibility", "Capacity, deadline, custom-product or delivery commitment exceptions.", LIGHT_AMBER),
        ],
        [
            info_card("Pricing", "Valid calculated prices that exceed approval thresholds, discounts or margin rules.", LIGHT_AMBER),
            info_card("PO", "Internal review of minor or policy-sensitive PO mismatches.", LIGHT_AMBER),
        ],
    ],
    colWidths=[82 * mm, 82 * mm],
    hAlign="LEFT",
)
approval_cards.setStyle(
    TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
)
story += [
    approval_cards,
    Spacer(1, 6 * mm),
    P(
        "Missing fundamental pricing inputs are now classified as blocked:pricing_data, "
        "not as an approvable pricing exception. This prevents human approval from bypassing "
        "a missing cost, tax, margin, discount or transport record.",
        "Body",
    ),
    PageBreak(),
]

# Technical architecture
story += [
    P("3. Technical architecture", "SectionTitle"),
    P(
        "The solution uses a compiled LangGraph as the orchestration layer, FastAPI as the "
        "application boundary, Gemini for selected language tasks, Qdrant for document "
        "retrieval and SQLite for local operational records.",
        "Body",
    ),
]

arch = Table(
    [
        [P("API layer", "TableHeader"), P("FastAPI endpoints for uploads, inquiries, events, approvals and diagnostics", "TableCell")],
        [P("Orchestration", "TableHeader"), P("LangGraph state machine with trigger routing and in-memory checkpoints", "TableCell")],
        [P("Agent layer", "TableHeader"), P("Specialized inquiry, catalog, qualification, inventory, feasibility, pricing, quotation, follow-up, negotiation, PO and handoff modules", "TableCell")],
        [P("Reasoning", "TableHeader"), P("Gemini structured output for extraction, classification, gap analysis, narrative and PO parsing", "TableCell")],
        [P("Knowledge", "TableHeader"), P("Local sentence-transformer embeddings and local persisted Qdrant collection", "TableCell")],
        [P("Structured data", "TableHeader"), P("Exact Qdrant payload-filter retrieval by business, agent, filename, type, active status and latest version", "TableCell")],
        [P("Persistence", "TableHeader"), P("SQLite for leads, audit logs, quotations, POs, sales orders and handoff records", "TableCell")],
    ],
    colWidths=[42 * mm, 123 * mm],
)
arch.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (0, -1), NAVY),
            ("TEXTCOLOR", (0, 0), (0, -1), WHITE),
            ("BACKGROUND", (1, 0), (1, -1), LIGHT_GREY),
            ("BOX", (0, 0), (-1, -1), 0.6, MID_GREY),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
    )
)
story += [arch, Spacer(1, 10 * mm)]

story += [
    P("Dual retrieval model", "Subsection"),
    P(
        "The architecture now separates semantic evidence retrieval from deterministic "
        "structured lookup. This corrects the earlier failure mode where top-K similarity "
        "search could retrieve a document but miss the exact required CSV row.",
        "Body",
    ),
]
dual = Table(
    [
        [P("Semantic RAG", "TableHeader"), P("Exact structured retrieval", "TableHeader")],
        [
            P(
                "Used for product descriptions, technical evidence, customer notes, terms, "
                "templates, objection guidance and narrative grounding.",
                "TableCell",
            ),
            P(
                "Used for product code, inventory, capacity, delivery city, price, product "
                "cost, margin, GST, transport zone and discount band.",
                "TableCell",
            ),
        ],
        [
            P("Embedding query, allowed document policy, similarity score and top-K result.", "TableCell"),
            P("Payload filters, no embedding score, complete document chunks and deterministic parsing.", "TableCell"),
        ],
    ],
    colWidths=[82.5 * mm, 82.5 * mm],
)
dual.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (0, 0), BLUE),
            ("BACKGROUND", (1, 0), (1, 0), TEAL),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("BACKGROUND", (0, 1), (0, -1), LIGHT_BLUE),
            ("BACKGROUND", (1, 1), (1, -1), LIGHT_TEAL),
            ("BOX", (0, 0), (-1, -1), 0.7, MID_GREY),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
    )
)
story += [dual, PageBreak()]

# Data and APIs
story += [
    P("4. Data, isolation and API surface", "SectionTitle"),
    P(
        "Documents are uploaded with business ownership, document type, agent access, "
        "version and active status. Agent policies restrict which document types each "
        "sub-agent may retrieve.",
        "Body",
    ),
]

data_rows = [
    [P("Data domain", "TableHeader"), P("Documents currently available", "TableHeader"), P("Decision key", "TableHeader")],
    [P("Catalog", "TableCellBold"), P("Product catalog and technical descriptions", "TableCell"), P("Product code and specifications", "TableCell")],
    [P("Customer", "TableCellBold"), P("CRM, payment behavior, order and quotation history", "TableCell"), P("Company/customer identity", "TableCell")],
    [P("Operations", "TableCellBold"), P("Inventory, capacity, delivery zones", "TableCell"), P("Product code and normalized city", "TableCell")],
    [P("Commercial", "TableCellBold"), P("Price, product cost, margin, GST, transport, discount and payment terms", "TableCell"), P("Product, zone, customer type and order value", "TableCell")],
    [P("Documents", "TableCellBold"), P("Quotation templates, terms and conditions", "TableCell"), P("Agent-scoped semantic evidence", "TableCell")],
]
story += [table(data_rows, [35 * mm, 82 * mm, 48 * mm]), Spacer(1, 10 * mm)]

api_rows = [
    [P("Endpoint", "TableHeader"), P("Purpose", "TableHeader")],
    [P("GET /healthz", "TableCellBold"), P("Reports Qdrant, RAG, graph, Gemini and database readiness.", "TableCell")],
    [P("POST /documents/upload", "TableCellBold"), P("Saves, parses, chunks, embeds and indexes an authorized business document.", "TableCell")],
    [P("POST /rag/retrieve", "TableCellBold"), P("Tests semantic agent-scoped retrieval.", "TableCell")],
    [P("POST /rag/document", "TableCellBold"), P("Tests exact structured document retrieval without embeddings.", "TableCell")],
    [P("POST /inquiries/process", "TableCellBold"), P("Creates a stable thread ID and runs the inquiry pipeline.", "TableCell")],
    [P("POST /pipeline/events", "TableCellBold"), P("Processes follow-up, customer reply or PO events on an existing thread.", "TableCell")],
    [P("POST /pipeline/approve", "TableCellBold"), P("Approves only the exact pending business stage and blocks unsafe pricing bypass.", "TableCell")],
]
story += [
    P("Current API surface", "Subsection"),
    table(api_rows, [55 * mm, 110 * mm]),
    Spacer(1, 10 * mm),
    P("State and auditability", "Subsection"),
    bullet("A thread ID correlates all events for one lead while the server remains running."),
    bullet("The graph records completed stages, approval reasons, pipeline status and outbound content."),
    bullet("RAG audit records capture agent, query, chunk IDs, document IDs and similarity scores."),
    bullet("Pricing records retain calculation logic and missing-input validation details."),
    bullet("Operational actions are written to the SQL audit log."),
    PageBreak(),
]

# Validation
story += [
    P("5. Validated scenario and controls", "SectionTitle"),
    P(
        "A live end-to-end scenario was executed against the local API after the exact "
        "structured retrieval upgrade.",
        "Body",
    ),
]

validated = [
    [P("Test input", "TableHeader"), P("Observed result", "TableHeader")],
    [P("Product", "TableCellBold"), P("MSB-076 - Steel Billet Model 076, IS2062 E250, size 100mm", "TableCell")],
    [P("Quantity and destination", "TableCellBold"), P("20 MT to Delhi", "TableCell")],
    [P("Requirement", "TableCellBold"), P("Exact match", "TableCell")],
    [P("Feasibility", "TableCellBold"), P("From stock; delivery zone North; location found", "TableCell")],
    [P("Pricing", "TableCellBold"), P("Pricing possible; final ex-GST price INR 104,870.44/MT; invoice INR 2,349,097.80", "TableCell")],
    [P("Quotation", "TableCellBold"), P("Quotation generated and saved", "TableCell")],
    [P("PO", "TableCellBold"), P("Minor mismatch on special conditions; PO approval requested", "TableCell")],
    [P("Final result", "TableCellBold"), P("handed_off; order won; sales order and handoff IDs created", "TableCell")],
    [P("Departments", "TableCellBold"), P("Production, inventory, purchase, dispatch and finance", "TableCell")],
]
story += [table(validated, [54 * mm, 111 * mm]), Spacer(1, 10 * mm)]

control_rows = [
    [P("Control", "TableHeader"), P("Current behavior", "TableHeader")],
    [P("Missing inquiry data", "TableCellBold"), P("Customer follow-up request and pipeline pause.", "TableCell")],
    [P("Unsupported/custom product", "TableCellBold"), P("Technical or human review before pricing.", "TableCell")],
    [P("Insufficient fulfillment", "TableCellBold"), P("Feasibility approval or inability-to-fulfill result.", "TableCell")],
    [P("Missing pricing inputs", "TableCellBold"), P("blocked:pricing_data; no quotation and no approval bypass.", "TableCell")],
    [P("Large order/discount/margin", "TableCellBold"), P("Pricing approval only after a valid price exists.", "TableCell")],
    [P("Critical PO mismatch", "TableCellBold"), P("Customer correction request; order is not won.", "TableCell")],
    [P("Minor PO mismatch", "TableCellBold"), P("Controlled PO approval before sales-order creation.", "TableCell")],
]
story += [
    P("Safety controls", "Subsection"),
    table(control_rows, [55 * mm, 110 * mm]),
    PageBreak(),
]

# Limitations and roadmap
story += [
    P("6. Current limitations and production roadmap", "SectionTitle"),
    P(
        "The system is suitable for a supervised local pilot. The following items should be "
        "completed before it sends binding commercial commitments without human oversight.",
        "Body",
    ),
]

limitations = [
    [P("Priority", "TableHeader"), P("Limitation", "TableHeader"), P("Recommended action", "TableHeader")],
    [P("High", "TableCellBold"), P("MemorySaver checkpoints disappear when Uvicorn restarts.", "TableCell"), P("Use PostgreSQL-backed LangGraph checkpoints and expose thread history.", "TableCell")],
    [P("High", "TableCellBold"), P("Generated product-cost and normalized-discount data are synthetic test records.", "TableCell"), P("Replace with approved BOM-based product costs and signed commercial policies.", "TableCell")],
    [P("High", "TableCellBold"), P("Message dispatch is mocked.", "TableCell"), P("Integrate authorized email/WhatsApp providers with delivery receipts and retry controls.", "TableCell")],
    [P("High", "TableCellBold"), P("Local embedded Qdrant permits only one process to hold the storage lock.", "TableCell"), P("Run Qdrant Server for concurrent workers and production deployment.", "TableCell")],
    [P("Medium", "TableCellBold"), P("Document version selection uses active status and latest version naming.", "TableCell"), P("Add an explicit uploaded_at field, effective dates and automatic deactivation of replaced versions.", "TableCell")],
    [P("Medium", "TableCellBold"), P("Requirement matching remains LLM-assisted when an explicit deterministic catalog key is unavailable.", "TableCell"), P("Add direct product-code extraction and normalized specification comparison before semantic fallback.", "TableCell")],
    [P("Medium", "TableCellBold"), P("Natural-language dates and encoding may produce null deadlines or mojibake.", "TableCell"), P("Normalize dates to ISO, enforce UTF-8 end to end and add locale-aware tests.", "TableCell")],
    [P("Medium", "TableCellBold"), P("Approval is an API action without authenticated user identity or role enforcement.", "TableCell"), P("Add authentication, RBAC, approver identity, comments, timestamps and immutable audit trails.", "TableCell")],
    [P("Medium", "TableCellBold"), P("No automated regression suite currently proves every branch.", "TableCell"), P("Add unit, graph-route, API, document-schema and end-to-end tests for all approval and mismatch cases.", "TableCell")],
]
story += [table(limitations, [20 * mm, 69 * mm, 76 * mm]), Spacer(1, 9 * mm)]

roadmap = Table(
    [
        [P("Phase 1 - Controlled pilot", "TableHeader"), P("Phase 2 - Operational integration", "TableHeader"), P("Phase 3 - Production scale", "TableHeader")],
        [
            P("Use verified master data, supervised approvals, persistent checkpoints and automated regression tests.", "TableCell"),
            P("Connect CRM/ERP, inventory, email/WhatsApp, identity and approval roles.", "TableCell"),
            P("Deploy Qdrant Server, PostgreSQL, workers, monitoring, security controls and disaster recovery.", "TableCell"),
        ],
    ],
    colWidths=[55 * mm] * 3,
)
roadmap.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), TEAL),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("BACKGROUND", (0, 1), (-1, 1), LIGHT_TEAL),
            ("BOX", (0, 0), (-1, -1), 0.6, TEAL),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, MID_GREY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]
    )
)
story += [
    P("Recommended roadmap", "Subsection"),
    roadmap,
    Spacer(1, 4 * mm),
    P("Bottom line", "Subsection"),
    P(
        "Today, the agent can coordinate a complete supervised sales workflow from incoming "
        "requirement to operational handoff, with auditable calculations and controlled "
        "approvals. Its strongest production feature is the separation of language reasoning "
        "from deterministic business data. Its next milestone is replacing local/test "
        "infrastructure and synthetic master data with persistent, authenticated and "
        "enterprise-integrated services.",
        "Body",
    ),
]

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
doc.build(
    story,
    onFirstPage=header_footer,
    onLaterPages=header_footer,
)
print(OUTPUT.resolve())
