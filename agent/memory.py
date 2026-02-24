import time, uuid
import httpx
from fastapi import APIRouter
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL = "http://qdrant:6333"
OLLAMA_URL = "http://ollama:11434"
COLLECTION = "memory"

router = APIRouter(prefix="/memory", tags=["memory"])
qdrant = QdrantClient(url=QDRANT_URL)

async def embed(text: str):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text}
        )
        return r.json()["embedding"]

def ensure_collection(dim):
    cols = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION in cols:
        return
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )

@router.post("/upsert")
async def upsert(body: dict):
    text = body.get("text", "")
    meta = body.get("meta", {})
    vec = await embed(text)
    ensure_collection(len(vec))
    pid = str(uuid.uuid4())

    qdrant.upsert(
        collection_name=COLLECTION,
        points=[PointStruct(
            id=pid,
            vector=vec,
            payload={"text": text, "meta": meta, "ts": time.time()}
        )],
    )
    return {"ok": True}

@router.post("/search")
async def search(body: dict):
    vec = await embed(body.get("query", ""))

    hits = qdrant.query_points(
        collection_name=COLLECTION,
        query=vec,
        limit=5,
        with_payload=True
    )

    return {"items": [p.payload for p in hits.points]}
