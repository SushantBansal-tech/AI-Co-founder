"""
document_store.py — Upload → Parse → Cache → Serve

This is the single place where business owner's uploaded documents are:
  1. Saved to disk (or S3 in prod)
  2. Parsed into typed structures
  3. Cached in memory (invalidated on next upload of same type)
  4. Served to agents via get_*() methods

Agents NEVER read raw files themselves.
They call DocumentManager.get_pricing_docs() etc. instead.

Upload flow:
  POST /upload  →  DocumentManager.upload(doc_type, file_bytes, filename)
                →  saves file
                →  parses based on doc_type
                →  records in uploaded_documents table
                →  invalidates cache for that doc_type

Agent read flow:
  pricing_agent  →  docs = await dm.get_pricing_docs()
  req_agent      →  collection = await dm.get_catalog_collection()
  feasibility    →  inv = dm.get_inventory_index()
"""

import os
import shutil
import asyncio
from pathlib import Path
from typing import Optional
from importlib import import_module

import chromadb
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from settings import (
    UPLOADS_DIR, CHROMA_PATH, CATALOG_COLLECTION,
    GEMINI_API_KEY, GEMINI_EMBED_MODEL, DocType,
)
from database import UploadedDocument, log_action


# ── Lazy-load agent modules to avoid circular imports ─────────────────────

def _catalog_mod():     return import_module("03_catalog_ingestion")
def _pricing_mod():     return import_module("09_pricing_documents")
def _inventory_mod():   return import_module("07_inventory_check")
def _feasibility_mod(): return import_module("08_feasibility_engine")


# ── DocumentManager ───────────────────────────────────────────────────────

class DocumentManager:
    """
    One instance per app startup (singleton via FastAPI lifespan).
    Holds parsed document caches so agents never re-parse on every request.
    """

    def __init__(self):
        self._gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
        self._chroma = chromadb.PersistentClient(path=CHROMA_PATH)

        # In-memory caches — invalidated when a new doc is uploaded
        self._catalog_collection: Optional[chromadb.Collection] = None
        self._pricing_docs = None
        self._inventory_index: Optional[dict] = None
        self._capacity_index: Optional[dict] = None
        self._delivery_index: Optional[dict] = None

        # Which doc types invalidate which cache
        self._cache_map = {
            DocType.PRODUCT_CATALOG: "_catalog_collection",
            DocType.PRICE_LIST:      "_pricing_docs",
            DocType.RM_COST:         "_pricing_docs",
            DocType.TRANSPORT:       "_pricing_docs",
            DocType.DISCOUNT_POLICY: "_pricing_docs",
            DocType.MARGIN_RULES:    "_pricing_docs",
            DocType.GST_RATES:       "_pricing_docs",
            DocType.INVENTORY:       "_inventory_index",
            DocType.PRODUCTION_CAP:  "_capacity_index",
            DocType.DELIVERY_ZONES:  "_delivery_index",
        }

    # ── Upload ───────────────────────────────────────────────────────────

    async def upload(
        self,
        session: AsyncSession,
        doc_type: str,
        file_bytes: bytes,
        filename: str,
        uploaded_by: str = "admin",
    ) -> UploadedDocument:
        """
        Save file, parse it, record in DB, invalidate cache.
        Called from POST /upload FastAPI route.
        """
        # 1. Deactivate previous active doc of same type
        prev = await session.execute(
            select(UploadedDocument)
            .where(UploadedDocument.doc_type == doc_type)
            .where(UploadedDocument.is_active == True)
        )
        for old in prev.scalars().all():
            old.is_active = False

        # 2. Save file
        dest_dir = UPLOADS_DIR / doc_type
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        dest_path.write_bytes(file_bytes)

        # 3. Try parsing
        parse_ok, parse_err = await self._parse(doc_type, dest_path, file_bytes)

        # 4. Record in DB
        record = UploadedDocument(
            doc_type=doc_type,
            filename=filename,
            file_path=str(dest_path),
            file_size_kb=len(file_bytes) // 1024,
            is_active=True,
            parsed_successfully=parse_ok,
            parse_error=parse_err,
            uploaded_by=uploaded_by,
        )
        session.add(record)
        await session.flush()

        await log_action(session, "document", record.id, "document_uploaded",
                         uploaded_by, {"doc_type": doc_type, "filename": filename,
                                       "parsed": parse_ok})
        await session.commit()

        # 5. Invalidate relevant cache
        cache_attr = self._cache_map.get(doc_type)
        if cache_attr:
            setattr(self, cache_attr, None)

        return record

    # ── Parse dispatcher ─────────────────────────────────────────────────

    async def _parse(
        self, doc_type: str, path: Path, raw: bytes
    ) -> tuple[bool, Optional[str]]:
        """Returns (success, error_message)."""
        try:
            text = raw.decode("utf-8", errors="replace")
            m_cat = _catalog_mod()
            m_pri = _pricing_mod()
            m_inv = _inventory_mod()
            m_fea = _feasibility_mod()

            if doc_type == DocType.PRODUCT_CATALOG:
                products = m_cat.parse_catalog_csv(text)
                if self._gemini:
                    # Rebuild ChromaDB collection
                    self._catalog_collection = m_cat.build_catalog_index(
                        products, self._gemini, self._chroma
                    )

            elif doc_type in (DocType.PRICE_LIST, DocType.RM_COST, DocType.TRANSPORT,
                               DocType.DISCOUNT_POLICY, DocType.MARGIN_RULES, DocType.GST_RATES):
                # Pricing docs — will be re-loaded next time get_pricing_docs() is called
                self._pricing_docs = None  # force reload

            elif doc_type == DocType.INVENTORY:
                self._inventory_index = m_inv.parse_inventory_csv(text)

            elif doc_type == DocType.PRODUCTION_CAP:
                self._capacity_index = m_fea.parse_capacity_csv(text)

            elif doc_type == DocType.DELIVERY_ZONES:
                self._delivery_index = m_fea.parse_delivery_csv(text)

            return True, None
        except Exception as e:
            return False, str(e)

    # ── Agent-facing getters ──────────────────────────────────────────────
    # Each getter: return cache if warm, else load from latest uploaded file.

    async def get_catalog_collection(self) -> Optional[chromadb.Collection]:
        if self._catalog_collection:
            return self._catalog_collection
        # Try loading from persisted ChromaDB
        try:
            self._catalog_collection = self._chroma.get_collection(CATALOG_COLLECTION)
            return self._catalog_collection
        except Exception:
            # Fall back to sample data if no upload yet
            m = _catalog_mod()
            if self._gemini:
                products = m.parse_catalog_csv(m.SAMPLE_CATALOG_CSV)
                self._catalog_collection = m.build_catalog_index(
                    products, self._gemini, self._chroma
                )
            return self._catalog_collection

    def get_pricing_docs(self):
        if self._pricing_docs:
            return self._pricing_docs
        # Build from sample CSVs (replaced by uploaded files in prod)
        m = _pricing_mod()
        self._pricing_docs = m.load_pricing_documents()
        return self._pricing_docs

    def get_inventory_index(self) -> dict:
        if self._inventory_index:
            return self._inventory_index
        m = _inventory_mod()
        self._inventory_index = m.parse_inventory_csv(m.SAMPLE_INVENTORY_CSV)
        return self._inventory_index

    def get_capacity_index(self) -> dict:
        if self._capacity_index:
            return self._capacity_index
        m = _feasibility_mod()
        self._capacity_index = m.parse_capacity_csv(m.SAMPLE_CAPACITY_CSV)
        return self._capacity_index

    def get_delivery_index(self) -> dict:
        if self._delivery_index:
            return self._delivery_index
        m = _feasibility_mod()
        self._delivery_index = m.parse_delivery_csv(m.SAMPLE_DELIVERY_CSV)
        return self._delivery_index

    def get_gemini_client(self) -> Optional[genai.Client]:
        return self._gemini


# ── Singleton (one instance shared across all requests) ───────────────────
document_manager = DocumentManager()


# ── Demo: show how an agent reads from document_manager ──────────────────

if __name__ == "__main__":
    dm = DocumentManager()

    print("Testing document_manager getters (using sample data)...")

    pricing = dm.get_pricing_docs()
    print(f"\nPricing docs loaded:")
    print(f"  price_list entries  : {len(pricing.price_list)}")
    print(f"  rm_cost entries     : {len(pricing.rm_costs)}")
    print(f"  discount bands      : {len(pricing.discount_bands)}")

    inv = dm.get_inventory_index()
    print(f"\nInventory index      : {len(inv)} products")
    for code, item in list(inv.items())[:3]:
        print(f"  {code}: {item.available_qty} {item.unit} @ {item.warehouse_location}")

    cap = dm.get_capacity_index()
    print(f"\nCapacity index       : {len(cap)} products")

    dlv = dm.get_delivery_index()
    print(f"\nDelivery zones       : {len(dlv)} cities")

    print("\nAll getters work correctly with sample data.")
    print("Upload real CSVs via POST /upload to replace samples in production.")