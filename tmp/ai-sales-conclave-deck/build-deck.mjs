import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

import { buildSlide01 } from "./slide-01.mjs";
import { buildSlide06 } from "./slide-06.mjs";
import { buildSlide09 } from "./slide-09.mjs";
import { buildSlide11 } from "./slide-11.mjs";
import { buildSlide12 } from "./slide-12.mjs";
import { buildSlide13 } from "./slide-13.mjs";
import { buildSlide15 } from "./slide-15.mjs";
import { buildSlide17 } from "./slide-17.mjs";
import { buildSlide18 } from "./slide-18.mjs";
import { buildSlide23 } from "./slide-23.mjs";
import { buildSlide26 } from "./slide-26.mjs";

const OUTPUT = "C:/Users/susha/Downloads/AI/AI_Native_Sales_OS_Conclave_2026.pptx";
const PREVIEW_DIR = "C:/Users/susha/Downloads/AI/tmp/ai-sales-conclave-deck/rendered";

function addNotes(slide, talkTrack) {
  slide.speakerNotes.textFrame.setText(
    `${talkTrack}\n\n[Sources]\n- User-provided AI sales-agent architecture, implementation status and event brief, 8 August 2026.\n[/Sources]`,
  );
}

const presentation = Presentation.create({
  slideSize: { width: 1280, height: 720 },
});

// 1 — Cover
{
  const slide = buildSlide01(presentation, {
    title: "AI FUTURE READY CONCLAVE 2026 · AGRA",
    title2: "Jarvis for\nindustrial sales",
    title3: "An AI-native sales operations system and CRM for manufacturers, MSMEs and B2B companies · Concept & prototype overview",
  });
  addNotes(
    slide,
    "Open with the business outcome: an AI sales executive that supports the complete inquiry-to-order process while the business owner retains control. State clearly that this is a concept and prototype overview, not a production launch.",
  );
}

// 2 — Problem
{
  const slide = buildSlide06(presentation, {
    title: "Industrial sales still depends on fragmented manual work",
    body1: {
      titleHere: "Inquiries get scattered",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Website, email and WhatsApp leads enter different inboxes. Context is copied manually and follow-ups can be missed.",
    },
    body2: {
      titleHere: "Quotations take time",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Sales teams search spreadsheets, customer history, stock, transport and pricing rules before responding.",
    },
    body3: {
      titleHere: "Knowledge stays with people",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Negotiations, buying patterns and payment behaviour are often remembered by individuals—not by the business.",
    },
    footer1: "2",
  });
  addNotes(
    slide,
    "Describe the current reality for many MSMEs: the problem is not lack of customer demand; it is fragmented execution between channels, spreadsheets, employees and operational departments.",
  );
}

// 3 — Current versus future
{
  const slide = buildSlide11(presentation, {
    title: "AI-native CRM turns records into controlled action",
    body1: {
      topic: "The operating shift",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "A traditional CRM records what employees already did.",
      loremIpsumDolorSitAmetConsecteturAdipiscing2: "An AI-native CRM understands events, recommends the next step and performs permitted work through controlled business tools.",
    },
    body2: "TODAY · Disconnected tools",
    body3: "FUTURE · Owner-controlled AI",
    body4: {
      detailGoesHere: "Manual lead capture",
      detailGoesHere2: "Delayed follow-up",
      detailGoesHere3: "Customer memory in inboxes",
    },
    body5: {
      detailGoesHere: "Unified customer context",
      detailGoesHere2: "Workflow-driven execution",
      detailGoesHere3: "Approvals for sensitive actions",
    },
    footer1: "3",
  });
  addNotes(
    slide,
    "Position the product beyond chatbots. The system is intended to maintain business state, select the next action and execute only what company policy permits.",
  );
}

// 4 — Workflow
{
  const slide = buildSlide17(presentation, {
    title: "One persistent workflow connects inquiry, quotation and order",
    label1: "CAPTURE",
    label2: "DECIDE",
    label3: "CONVERT",
    body1: {
      titleHere: "Understand the opportunity",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Capture website, email and WhatsApp inquiries. Extract requirements, identify the customer and resolve the exact product.",
    },
    body2: {
      titleHere: "Validate and quote",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Check stock, capacity, delivery, pricing, margin, GST, transport and credit. Pause when owner approval is required.",
    },
    body3: {
      titleHere: "Follow through to handoff",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Send the quotation, follow up, understand replies, validate the PO, create the sales order and notify departments.",
    },
    footer1: "4",
  });
  addNotes(
    slide,
    "Walk left to right. Emphasize that the same customer and deal identity continues through the workflow instead of creating disconnected records at each stage.",
  );
}

// 5 — Safety and authority
{
  const slide = buildSlide09(presentation, {
    title: "AI autonomy remains inside company-defined commercial boundaries",
    body1: {
      topic: "The safety principle",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Gemini can understand messages and draft communication.",
      loremIpsumDolorSitAmetConsecteturAdipiscing2: "Deterministic rules decide money, authority, inventory, credit and order commitments.",
    },
    body2: {
      titleHere: "AI interprets",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Understands intent, summarizes context and drafts communication.",
    },
    body3: {
      titleHere: "Policy engine decides",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Checks exact data, authority limits, risk and required evidence.",
    },
    body4: {
      titleHere: "Owner controls exceptions",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Approves discounts, credit changes and PO exceptions.",
    },
    footer1: "5",
  });
  addNotes(
    slide,
    "Use a simple phrase: AI proposes, policies authorize, business tools execute, and the owner approves exceptions. This is the core trust model.",
  );
}

// 6 — Data architecture
{
  const slide = buildSlide15(presentation, {
    title: "The CRM becomes the business memory—not another isolated application",
    body1: {
      titleHere: "One business record",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Customers, interactions, quotations, orders, payments and AI actions remain connected by permanent identities.",
      quamUtMassaLuctusCursusNullamPharetra: "PostgreSQL holds exact facts; Qdrant supports semantic memory; LangGraph preserves long-running workflow state.",
    },
    label1: "POSTGRESQL",
    body2: "Customers · prices · inventory · orders",
    label2: "QDRANT",
    body3: "Documents · notes · narrative memory",
    label3: "LANGGRAPH",
    body4: "Workflow state · waiting reasons · resume",
    label4: "AUDIT",
    body5: "Who acted · why · policy · result",
    footer1: "6",
  });
  addNotes(
    slide,
    "Clarify the source-of-truth boundaries. PostgreSQL owns exact operational facts. Qdrant assists semantic retrieval. LangGraph tracks workflow execution. Important business outcomes are persisted in normal CRM tables.",
  );
}

// 7 — Owner command centre
{
  const slide = buildSlide12(presentation, {
    title: "Owner command centre: decisions, risks and outcomes",
    body1: {
      topic: "Founder command centre",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "A single interface focuses attention on what requires a human decision and what Jarvis completed independently.",
    },
    body2: {
      titleGoesHere: "Pending approvals",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Commercial impact, policy reason, evidence and expiry.",
    },
    body3: {
      titleGoesHere: "Revenue pipeline",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "RFQs, quotations, accepted terms, POs and orders.",
    },
    body4: {
      titleGoesHere: "Risks and attention",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Stalled deals, expiring quotes, payment and delivery risks.",
    },
    body5: {
      titleGoesHere: "Completed AI actions",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "What happened, why it happened and the audit reference.",
    },
    footer1: "7",
  });
  addNotes(
    slide,
    "Explain that the founder is not expected to operate every CRM field. The interface is designed around approvals, risks, pipeline value and completed AI work.",
  );
}

// 8 — Impact
{
  const slide = buildSlide13(presentation, {
    title: "The business impact is faster execution with stronger control",
    body1: {
      titleGoesHere: "Response discipline",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Fewer manual handoffs between inquiry, feasibility, pricing and quotation.",
    },
    body2: {
      titleGoesHere: "Revenue protection",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Consistent follow-ups, validity checks and visibility into stalled opportunities.",
    },
    body3: {
      titleGoesHere: "Commercial governance",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Deterministic calculations, configurable authority and human approval for exceptions.",
    },
    body4: {
      titleGoesHere: "Institutional memory",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Customer history and buying behaviour remain with the business as teams change.",
    },
    footer1: "8",
  });
  addNotes(
    slide,
    "Keep the claims qualitative because the prototype has not completed production pilots. These are the intended impact areas to measure with pilot customers.",
  );
}

// 9 — Business model
{
  const slide = buildSlide23(presentation, {
    title: "A focused pilot grows into a recurring platform",
    body1: {
      titleHere: "Discovery pilot",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Map one workflow, onboard core data and agree success measures.",
    },
    body2: {
      titleHere: "Platform subscription",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "AI sales agent, CRM memory, channels, approvals and management visibility.",
    },
    body3: {
      titleHere: "Integration & support",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "ERP connectors, custom policies, onboarding and operational support.",
    },
    label1: "ENTRY",
    label2: "RECURRING",
    label3: "EXPANSION",
    stat1: "Pilot",
    stat2: "SaaS",
    stat3: "Custom",
    label4: "Scoped",
    label5: "Tenant",
    label6: "Tailored",
    footer1: "9",
  });
  addNotes(
    slide,
    "Present a land-and-expand model without committing to final prices. Begin with paid workflow discovery, convert successful pilots into subscriptions, then add integrations and tailored support.",
  );
}

// 10 — Ideal customer
{
  const slide = buildSlide06(presentation, {
    title: "The first customers should have repeatable B2B sales complexity",
    body1: {
      titleHere: "Manufacturers",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Frequent RFQs, product specifications, inventory or capacity checks and multi-department handoff.",
    },
    body2: {
      titleHere: "MSMEs",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Owner-led sales, limited CRM discipline and heavy dependence on email, WhatsApp and spreadsheets.",
    },
    body3: {
      titleHere: "B2B operators",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Repeated quotation, negotiation, PO and follow-up processes with clear commercial policies.",
    },
    footer1: "10",
  });
  addNotes(
    slide,
    "Focus early customer discovery on companies with repeated quotation workflows and measurable delays. Avoid trying to serve every industry at once.",
  );
}

// 11 — Roadmap and honest readiness
{
  const slide = buildSlide18(presentation, {
    title: "Trust comes before autonomy",
    body1: {
      titleHere: "Prototype foundation",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "CRM data foundation, customer identity, channel ingestion, structured business data, persistent workflows and audit events.",
    },
    body2: {
      titleHere: "Owner-control layer",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "AI service identity, controlled tools, configurable authority, durable action approvals and the founder command centre.",
    },
    body3: {
      titleHere: "Pilot-led autonomy",
      loremIpsumDolorSitAmetConsecteturAdipiscing: "Validate with real workflows, measure outcomes and gradually enable low-risk automatic actions.",
    },
    label1: "",
    label2: "",
    label3: "",
    footer1: "11",
  });
  addNotes(
    slide,
    "Be transparent: the technical foundation exists, but the authority layer, polished command centre and production hardening remain the next steps. Trust is built before autonomy is expanded.",
  );
}

// 12 — Close
{
  const slide = buildSlide26(presentation, {
    title: "THE PILOT ASK",
    title2: "Let us map one\nreal sales workflow",
    title3: {
      loremIpsumDetails: "Manufacturers & MSMEs",
      loremIpsumDetails2: "Inquiry → quotation → PO",
      loremIpsumDetails3: "Discovery · validation · pilot",
    },
  });
  addNotes(
    slide,
    "Close with a specific request: invite manufacturers and B2B MSMEs to share an anonymized inquiry-to-order process for a short discovery session and potential pilot. Do not ask them to adopt an unfinished production system today.",
  );
}

await fs.mkdir(PREVIEW_DIR, { recursive: true });
for (const [index, slide] of presentation.slides.items.entries()) {
  const stem = `slide-${String(index + 1).padStart(2, "0")}`;
  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(`${PREVIEW_DIR}/${stem}.png`, new Uint8Array(await png.arrayBuffer()));
  const layout = await slide.export({ format: "layout" });
  await fs.writeFile(`${PREVIEW_DIR}/${stem}.layout.json`, await layout.text(), "utf8");
}

const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
await fs.writeFile(`${PREVIEW_DIR}/montage.webp`, new Uint8Array(await montage.arrayBuffer()));

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(OUTPUT);

console.log(OUTPUT);
