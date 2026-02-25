import os
import shlex
from typing import Any

import httpx
from fastapi import FastAPI, APIRouter
import jobs_min
import jobs_ai
from pydantic import BaseModel
from openai import AsyncOpenAI

app = FastAPI()

BASH_BRIDGE_URL = os.environ.get("BASH_BRIDGE_URL", "http://bash-bridge:8020")
if BASH_BRIDGE_URL.endswith("/run"):
    BASH_BRIDGE_URL = BASH_BRIDGE_URL[:-4]

class AgentReq(BaseModel):
    chatInput: str
    mode: str = "openai"
    maxLoops: int | None = 8

class BashReq(BaseModel):
    cmd: str
    timeout: int | None = None

class LogsReq(BaseModel):
    service: str
    tail: int = 200

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/version")
async def version():
    return {"version":"0.1"}

def _openai_tools_spec() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a command via bash-bridge. Provide cmd as a plain string like: ls -la",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["cmd"],
                },
            },
        }
    ]

def _to_tokens(cmd: str) -> list[str]:
    # robust parsing (handles quotes)
    return shlex.split(cmd)

async def _bashbridge_run(tokens: list[str], timeout: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"cmd": tokens}
    if timeout is not None:
        payload["timeout"] = timeout

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BASH_BRIDGE_URL}/run", json=payload)
        if r.status_code >= 400:
            return {"bash_bridge_status": r.status_code, "bash_bridge_text": r.text, "sent_payload": payload}
        return r.json()

async def _tool_bash(cmd: str, timeout: int | None = None) -> dict[str, Any]:
    return await _bashbridge_run(_to_tokens(cmd), timeout=timeout)

@app.post("/bash")
async def bash_api(req: BashReq):
    # Direct API (no LLM)
    return await _tool_bash(req.cmd, req.timeout)

@app.get("/status")
async def status():
    import docker
    client = docker.DockerClient(base_url="unix://var/run/docker.sock")
    rows = []
    for c in client.containers.list(all=True):
        try:
            img = (c.image.tags[0] if c.image.tags else c.image.short_id)
        except Exception:
            img = "unknown"
        rows.append({"name": c.name, "status": c.status, "image": img})
    rows.sort(key=lambda x: x["name"])
    return {"containers": rows}

@app.get("/logs/{service}")
async def logs(service: str, tail: int = 200):
    import docker
    client = docker.DockerClient(base_url="unix://var/run/docker.sock")
    c = client.containers.get(service)
    data = c.logs(tail=tail)
    txt = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    return {"service": service, "tail": tail, "logs": txt}

@app.post("/smoke")
async def smoke():
    results: list[dict[str, Any]] = []
    # 1) router health
    results.append({"check": "router_health", "ok": True})

    # 2) bash-bridge simple commands
    results.append({"check": "bash_pwd", "result": await _bashbridge_run(["pwd"])})
    results.append({"check": "bash_ls", "result": await _bashbridge_run(["ls", "-la"])})

    # 3) docker ps (ensures docker control path works)
    import docker
    client = docker.DockerClient(base_url="unix://var/run/docker.sock")
    names = [c.name for c in client.containers.list(all=True)]
    results.append({"check": "docker_ps", "result": {"containers": names}})

    # overall
    ok = True
    for r in results:
        if "result" in r and isinstance(r["result"], dict) and r["result"].get("bash_bridge_status"):
            ok = False
    return {"ok": ok, "results": results}

async def run_openai_with_tools(model: str, user_text: str, max_loops: int = 8) -> tuple[str, list[dict[str, Any]]]:
    client = AsyncOpenAI()
    tools = _openai_tools_spec()

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "És um agente executor. Se precisares correr comandos, usa a tool bash. "
                "Quando chamares bash, passa só o comando (ex: 'ls -la', 'pwd')."
            ),
        },
        {"role": "user", "content": user_text},
    ]

    steps: list[dict[str, Any]] = []

    for _ in range(max_loops):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            return (msg.content or "", steps)

        messages.append(msg.model_dump())

        for tc in tool_calls:
            fn = tc.function.name
            args = tc.function.arguments
            try:
                import json as _json
                parsed = _json.loads(args) if isinstance(args, str) else (args or {})
            except Exception:
                parsed = {"cmd": str(args)}

            if fn == "bash":
                out = await _tool_bash(parsed.get("cmd", ""), parsed.get("timeout"))
            else:
                out = {"error": f"unknown tool {fn}"}

            steps.append({"tool": fn, "input": parsed, "output": out})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": fn,
                    "content": str(out),
                }
            )

    return ("Max loops reached.", steps)

@app.post("/agent")
async def agent(req: AgentReq):
    if req.mode != "openai":
        return {"status": "error", "mode_used": req.mode, "answer": "Only openai mode is enabled in SAFE router.", "steps": []}

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    try:
        answer, steps = await run_openai_with_tools(model, req.chatInput, req.maxLoops or 8)
        return {"status": "ok", "mode_used": "openai", "answer": answer, "steps": steps}
    except Exception as e:
        return {"status": "error", "mode_used": "openai", "answer": f"OPENAI_ERR: {e!r}", "steps": []}

@app.post("/autopilot")
async def autopilot(payload: dict[str, Any]):
    """
    Minimal autopilot:
      - input: {"goal": "...", "mode":"openai", "model":"gpt-4.1-mini"} (mode/model optional)
      - output: {"ok": bool, "answer": str, "steps": [...], "error": "..."}
    """
    goal = (payload.get("goal") or "").strip()
    if not goal:
        return {"ok": False, "error": "missing goal"}

    mode = (payload.get("mode") or "openai").strip()
    model = (payload.get("model") or os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini").strip()

    if mode != "openai":
        return {"ok": False, "error": f"unsupported mode: {mode}"}

    try:
        answer, steps = await run_openai_with_tools(model=model, user_text=goal)
        return {"ok": True, "mode_used": mode, "model": model, "answer": answer, "steps": steps}
    except Exception as e:
        return {"ok": False, "mode_used": mode, "model": model, "error": str(e)}


# --- AIOS JOBS MIN ---
_jobs = APIRouter()

@_jobs.post("/jobs/dev")
def jobs_dev(payload: dict):
    return jobs_ai.new_job(payload) if payload.get("ai") else jobs_min.new_job(payload)

@_jobs.post("/jobs/list")
def jobs_list(payload: dict = None):
    return jobs_min.list_jobs()

try:
    app.include_router(_jobs)
except Exception:
    pass


# ── BACKLOG ───────────────────────────────────────────────────────────────────
from backlog import add_task, list_tasks, get_next_task, update_task

@app.post("/backlog/add")
def backlog_add(body: dict):
    task = add_task(title=body["title"], goal=body["goal"], priority=body.get("priority", 5), task_type=body.get("type", "DEV_TASK"))
    return {"ok": True, "task": task}

@app.get("/backlog/list")
def backlog_list():
    return {"tasks": list_tasks()}

@app.get("/backlog/next")
def backlog_next():
    task = get_next_task()
    return {"task": task} if task else {"task": None, "message": "backlog empty"}

@app.post("/backlog/update")
def backlog_update(body: dict):
    task_id = body.pop("id")
    task = update_task(task_id, **body)
    return {"ok": bool(task), "task": task}

# ── AUTOPILOT STARTUP ─────────────────────────────────────────────────────────
import autopilot
from jobs_ai import new_job
import backlog as _backlog_mod

autopilot.start(new_job, _backlog_mod)
