"""
settings.py — single source of truth for all configuration.

Every agent imports from here. No agent hardcodes a value.
In production: all UPPERCASE vars come from environment / .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Project root ─────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# ── Database ──────────────────────────────────────────────────────────────
# Local dev  : sqlite+aiosqlite:///./sales_os.db
# Production : postgresql+asyncpg://user:pass@host:5432/sales_os
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{BASE_DIR / 'sales_os.db'}"
)

# ── Gemini ────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")

# ── ChromaDB ──────────────────────────────────────────────────────────────
# Local : persistent folder
# Prod  : swap to pgvector or Chroma Cloud
CHROMA_PATH            = str(BASE_DIR / "chroma_db")
CATALOG_COLLECTION     = "product_catalog"

# ── File upload ───────────────────────────────────────────────────────────
ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf", ".html", ".txt"}
MAX_UPLOAD_SIZE_MB        = 50

# Document type keys — used in uploaded_documents table
class DocType:
    PRODUCT_CATALOG   = "product_catalog"
    PRICE_LIST        = "price_list"
    RM_COST           = "rm_cost"
    TRANSPORT         = "transport"
    DISCOUNT_POLICY   = "discount_policy"
    MARGIN_RULES      = "margin_rules"
    GST_RATES         = "gst_rates"
    INVENTORY         = "inventory"
    PRODUCTION_CAP    = "production_capacity"
    DELIVERY_ZONES    = "delivery_zones"
    PAYMENT_TERMS     = "payment_terms"
    QUOTATION_TEMPLATE= "quotation_template"
    TNC               = "terms_and_conditions"
    CUSTOMER_CRM      = "customer_crm"

# ── Business thresholds ───────────────────────────────────────────────────
# All dollar/rupee values in INR

# Requirement matching
EXACT_SIMILARITY_THRESHOLD  = float(os.getenv("EXACT_THRESHOLD", "0.82"))
NEAR_SIMILARITY_THRESHOLD   = float(os.getenv("NEAR_THRESHOLD",  "0.65"))

# Customer qualification
HOT_SCORE_THRESHOLD   = int(os.getenv("HOT_SCORE",  "70"))
WARM_SCORE_THRESHOLD  = int(os.getenv("WARM_SCORE", "40"))
CREDIT_RISK_PCT       = float(os.getenv("CREDIT_RISK_PCT", "80"))   # flag above this %

# Pricing
LARGE_ORDER_THRESHOLD_INR = float(os.getenv("LARGE_ORDER_INR", "5000000"))  # ₹50L

# Quotation
QUOTATION_VALIDITY_DAYS = int(os.getenv("QUOTATION_VALIDITY_DAYS", "30"))

# ── Company info (shown on quotation) ────────────────────────────────────
COMPANY_NAME    = os.getenv("COMPANY_NAME",    "IndusSteel Trading Pvt. Ltd.")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "Plot 14, Industrial Area Phase II, Ludhiana - 141003")
COMPANY_GSTIN   = os.getenv("COMPANY_GSTIN",  "03AABCI1234A1Z5")
COMPANY_EMAIL   = os.getenv("COMPANY_EMAIL",  "sales@indussteel.in")
COMPANY_PHONE   = os.getenv("COMPANY_PHONE",  "+91-161-4567890")
COMPANY_BANK    = os.getenv("COMPANY_BANK",   "HDFC Bank, A/C: 50200012345678, IFSC: HDFC0001234")

# ── Dispatch ─────────────────────────────────────────────────────────────
SENDGRID_API_KEY       = os.getenv("SENDGRID_API_KEY", "")
WHATSAPP_API_TOKEN     = os.getenv("WHATSAPP_API_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
FROM_EMAIL             = os.getenv("FROM_EMAIL", "sales@indussteel.in")

# ── Debug ─────────────────────────────────────────────────────────────────
DEBUG = os.getenv("DEBUG", "false").lower() == "true"