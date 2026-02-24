import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/agent", tags=["agent"])
AGENT_CORE = "http://localhost:8010"

async def think(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{AGENT_CORE}/think", json={"prompt": prompt})
        r.raise_for_status()
        return r.json().get("text", "")

async def memory_search(query: str):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{AGENT_CORE}/memory/search", json={"query": query})
        r.raise_for_status()
        return r.json().get("items", [])

async def create_event(type_: str, payload: dict):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AGENT_CORE}/events",
            json={"source": "agent", "type": type_, "payload": payload},
        )
        r.raise_for_status()
        return r.json()

@router.post("/run")
async def run_agent(body: dict):
    goal = body.get("goal", "") or ""

    memories = await memory_search(goal)

    plan_prompt = f"Objetivo: {goal}\nMemória relevante: {memories}\nCria um plano curto de ações."
    plan = await think(plan_prompt)

    # guardar plano na memória
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AGENT_CORE}/memory/upsert",
            json={"text": f"GOAL: {goal}\nPLAN: {plan}", "meta": {"kind": "agent_plan"}},
        )
        r.raise_for_status()

    # criar evento de watch
    await create_event(
        "watch_create",
        {
            "goal": goal,
            "watch": "granulator_stop",
            "condition": {"type": "machine_status", "machine": "granulator", "status": "stopped"},
            "action": {"type": "alert", "message": "Granulador parou"},
        },
    )

    await create_event("plan_created", {"goal": goal, "plan": plan})

    return {"goal": goal, "plan": plan}
