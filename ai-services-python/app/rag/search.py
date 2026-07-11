import os
import io
import uuid
import pypdf
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import google.generativeai as genai

# Initialize the Qdrant client in memory (100% free, local)
client = QdrantClient(":memory:")
COLLECTION_NAME = "catalog_products"
VECTOR_DIMENSION = 768  # Dimension size for models/text-embedding-004

def initialize_collection():
    client.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_DIMENSION, distance=Distance.COSINE),
    )

def embed_text(text: str, is_query: bool = False) -> list:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Static mock vector fallback for offline tests
        return [0.1] * VECTOR_DIMENSION

    genai.configure(api_key=api_key)
    task_type = "retrieval_query" if is_query else "retrieval_document"
    try:
        result = genai.embed_content(
            model="models/text-embedding-004",
            content=text,
            task_type=task_type
        )
        return result["embedding"]
    except Exception as e:
        # Fallback in case of rate limits or transient issues
        return [0.1] * VECTOR_DIMENSION

def ingest_pdf(pdf_bytes: bytes) -> dict:
    # 1. Clear and create the Qdrant collection
    initialize_collection()

    # 2. Parse PDF content
    pdf_file = io.BytesIO(pdf_bytes)
    reader = pypdf.PdfReader(pdf_file)
    
    indexed_count = 0
    points = []

    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = text.strip()
        if not text:
            continue

        # 3. Create vector embedding
        vector = embed_text(text, is_query=False)

        # 4. Create Qdrant indexing point
        point_id = str(uuid.uuid4())
        points.append(PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "page_number": i + 1,
                "content": text
            }
        ))
        indexed_count += 1

    # 5. Insert points into Qdrant index
    if points:
        client.upsert(
            collection_name=COLLECTION_NAME,
            wait=True,
            points=points
        )

    return {
        "success": True,
        "indexed_pages": indexed_count,
        "collection_name": COLLECTION_NAME
    }

def search_catalog(query: str, limit: int = 3) -> list:
    # Check if the collection exists
    collections = client.get_collections().collections
    collection_exists = any(c.name == COLLECTION_NAME for c in collections)
    if not collection_exists:
        return []

    # 1. Generate query vector embedding
    query_vector = embed_text(query, is_query=True)

    # 2. Perform Cosine Similarity Search
    search_results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=limit
    )

    # 3. Format and return matched text payloads
    matches = []
    for hit in search_results:
        matches.append({
            "page_number": hit.payload.get("page_number"),
            "content": hit.payload.get("content"),
            "score": hit.score
        })
    return matches
