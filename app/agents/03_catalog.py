"""
Sub-problem 5: Represent the product catalog so it can be matched against.

Responsibilities:
  1. Parse catalog CSV into typed CatalogProduct objects
  2. Build a rich embed text per product (name + grade + specs combined)
  3. Generate embeddings via Gemini text-embedding-004
  4. Store in ChromaDB with metadata (swap to pgvector in prod)
  5. Expose query_catalog(text) → [(CatalogProduct, similarity_score)]

Design notes:
  - build_catalog_index() is called ONCE per catalog upload/update event, not per query.
  - query_catalog() is called at runtime per inquiry — it only embeds the query text.
  - similarity = 1 - cosine_distance, so 1.0 = identical, 0.0 = unrelated.

Run:
    GEMINI_API_KEY=xxx python 03_catalog_ingestion.py
"""

import io
import csv
import os
from typing import Optional

import chromadb
from google import genai
from pydantic import BaseModel


COLLECTION_NAME = "product_catalog"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class CatalogProduct(BaseModel):
    product_code: str
    name: str
    category: str
    grade: Optional[str] = None
    specifications: Optional[str] = None
    unit: str = "MT"

    def to_embed_text(self) -> str:
        """
        Combines name + category + grade + specs into one string for embedding.
        Richer combined text gives better semantic matches than name alone.
        """
        parts = [self.name, self.category]
        if self.grade:
            parts.append(f"Grade {self.grade}")
        if self.specifications:
            parts.append(self.specifications)
        return " | ".join(parts)

    def to_metadata(self) -> dict:
        """ChromaDB metadata must be flat dict of str/int/float/bool only."""
        return {k: (v or "") for k, v in self.model_dump().items()}


# ---------------------------------------------------------------------------
# Sample catalog (replace with upload/parse from real CSV/Excel in prod)
# ---------------------------------------------------------------------------

SAMPLE_CATALOG_CSV = """\
product_code,name,category,grade,specifications,unit
MSB-001,MS Billet,Steel Billet,IS2062,100x100mm to 150x150mm square section prime quality,MT
MSP-001,MS Plate,Steel Plate,IS2062,Thickness 6mm to 100mm width up to 3000mm hot rolled,MT
MSA-001,MS Angle,Structural Steel,IS2062,25x25mm to 200x200mm equal and unequal angles,MT
PIP-001,MS Pipe,Steel Pipe,IS1239,15mm to 150mm NB ERW pipes medium and heavy class,MT
TMT-001,TMT Bar,Reinforcement Bar,IS1786 Fe500,8mm to 32mm diameter ribbed TMT bars,MT
IBE-001,I Beam,Structural Steel,IS2062,100mm to 600mm ISMB sections standard length 12m,MT
CHN-001,Channel Section,Structural Steel,IS2062,75mm to 400mm ISMC sections standard length 12m,MT
SHT-001,MS Sheet,Steel Sheet,IS513,0.5mm to 6mm CR and HR sheets coil and cut length,MT
"""


def parse_catalog_csv(csv_text: str) -> list[CatalogProduct]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return [
        CatalogProduct(
            product_code=row["product_code"],
            name=row["name"],
            category=row["category"],
            grade=row.get("grade") or None,
            specifications=row.get("specifications") or None,
            unit=row.get("unit", "MT"),
        )
        for row in reader
    ]


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def get_embedding(text: str, client: genai.Client) -> list[float]:
    response = client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
    )
    return list(response.embeddings[0].values)


# ---------------------------------------------------------------------------
# Index build (run once per catalog upload)
# ---------------------------------------------------------------------------

def build_catalog_index(
    products: list[CatalogProduct],
    gemini_client: genai.Client,
    chroma_client: chromadb.Client,
) -> chromadb.Collection:
    """
    Wipes and rebuilds the collection from the current product list.
    Extend this to do incremental upserts if the catalog is very large.
    """
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass

    collection = chroma_client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},  # distances = 1 - cosine_similarity
    )

    ids, embeddings, documents, metadatas = [], [], [], []
    for p in products:
        text = p.to_embed_text()
        ids.append(p.product_code)
        embeddings.append(get_embedding(text, gemini_client))
        documents.append(text)
        metadatas.append(p.to_metadata())

    collection.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    print(f"[catalog] Indexed {len(products)} products.")
    return collection


# ---------------------------------------------------------------------------
# Runtime query (called per inquiry)
# ---------------------------------------------------------------------------

def query_catalog(
    query_text: str,
    collection: chromadb.Collection,
    gemini_client: genai.Client,
    n_results: int = 3,
) -> list[tuple[CatalogProduct, float]]:
    """
    Returns top-n (CatalogProduct, similarity) sorted best-first.
    similarity = 1 - cosine_distance → range [0.0, 1.0].
    """
    query_embedding = get_embedding(query_text, gemini_client)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(n_results, collection.count()),
        include=["metadatas", "distances"],
    )

    output = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        similarity = round(1.0 - dist, 4)
        output.append((CatalogProduct(**meta), similarity))
    return output


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    chroma_client = chromadb.Client()  # ephemeral; use PersistentClient(path=...) in prod

    products = parse_catalog_csv(SAMPLE_CATALOG_CSV)
    collection = build_catalog_index(products, gemini_client, chroma_client)

    test_cases = [
        ("MS Billet IS2062 grade 100x100mm square",   "expect: MSB-001 high score"),
        ("ERW steel pipe 2 inch NB medium class",     "expect: PIP-001 high score"),
        ("TMT Fe500 12mm diameter reinforcement bar", "expect: TMT-001 high score"),
        ("custom high-tensile alloy ASTM A36 billet", "expect: low score → CUSTOM"),
    ]

    for query, note in test_cases:
        print(f"\nQuery : {query!r}")
        print(f"Note  : {note}")
        matches = query_catalog(query, collection, gemini_client, n_results=2)
        for product, score in matches:
            print(f"  [{score:.3f}] {product.product_code}  {product.name}  (grade: {product.grade})")