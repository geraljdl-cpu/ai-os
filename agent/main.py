from agent_runner import router as agent_router
from memory import router as memory_router
import os, json
import asyncio
import asyncpg
import httpx
import aiomqtt
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any


app = FastAPI(title="AI-OS Agent Core")
app.include_router(memory_router)
app.include_router(agent_router)


@app.get("/health")
def health():
    return {"ok": True}

DB_URL = os.getenv("DATABASE_URL")
pool = None

@app.on_event("startup")
async def startup():
    global pool
    if DB_URL:
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)

        async def mqtt_loop():
            host = os.getenv("MQTT_HOST", "mosquitto")
            port = int(os.getenv("MQTT_PORT", "1883"))
            topic = os.getenv("MQTT_TOPIC", "factory/#")

            while True:
                try:
                    async with aiolient(host, port) as client:
                        await client.subscribe(topic)
                        async with client.unfiltered_messages() as messages:
                            async for msg in messages:
                                t = msg.topic.value if hasattr(msg.topic, "value") else str(msg.topic)
                                raw = msg.payload.decode("utf-8", "ignore")
                                try:
                                    payload = json.loads(raw)
                                except Exception:
                                    payload = {"raw": raw}

                                await pool.execute(
                                    "INSERT INTO events (source,type,trace_id,payload) VALUES ($1,$2,$3,$4)",
                                    "mqtt", t, None, json.dumps(payload),
                                )
                except Exception:
                    await asyncio.sleep(2)

        # asyncio.create_task(mqtt_loop())



@app.on_event("shutdown")
async def shutdown():
    global pool
    if pool:
        await pool.close()
        pool = None

class EventIn(BaseModel):
    source: str
    type: str
    trace_id: str | None = None
    payload: dict[str, Any] = {}

@app.post("/events")
async def create_event(e: EventIn):
    if not pool:
        return {"ok": False, "error": "db_not_configured"}

    row = await pool.fetchrow(
        "INSERT INTO events (source,type,trace_id,payload) VALUES ($1,$2,$3,$4) RETURNING id, ts",
        e.source, e.type, e.trace_id, json.dumps(e.payload),
    )
    return {"ok": True, "id": row["id"], "ts": str(row["ts"])}

@app.get("/events")
async def list_events(limit: int = 50):
    if not pool:
        return {"ok": False, "error": "db_not_configured"}

    rows = await pool.fetch(
        "SELECT id, ts, source, type, trace_id, payload FROM events ORDER BY ts DESC LIMIT $1",
        limit
    )

    return {"items": [dict(r) for r in rows]}

# ---------- LLM LOCAL (OLLAMA) ----------

async def think_local(prompt: str) -> str:
    base = os.getenv("OLLAMA_URL", "http://ollama:11434")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(
            f"{base}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False}
        )
        return r.json().get("response", "").strip()


# ---------- THINK COM MEMÓRIA (RAG) ----------

@app.post("/think")
async def think(body: dict):
    prompt = body.get("prompt") or ""

    memories = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "http://localhost:8010/memory/search",
                json={"query": prompt}
            )
            memories = r.json().get("items", [])
    except Exception:
        pass

    context = ""
    if memories:
        context = "\n\nMEMÓRIA RELEVANTE:\n"
        for m in memories:
            context += f"- {m['text']}\n"

    final_prompt = context + "\n\nPERGUNTA:\n" + prompt

    answer = await think_local(final_prompt)
    return {"text": answer}
