import os, time, json
from typing import Any, Dict, Optional, List

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

AGENT_ROUTER_URL = os.environ.get("AGENT_ROUTER_URL", "http://agent-router:5679").rstrip("/")
BASH_BRIDGE_URL = os.environ.get("BASH_BRIDGE_URL", "http://bash-bridge:8020").rstrip("/")
DEFAULT_MODE = os.environ.get("DEFAULT_MODE", "ollama")

app = FastAPI(title="orchestrator", version="0.2.0")

class ChatIn(BaseModel):
    chatInput: str = Field(..., min_length=1)
    mode: Optional[str] = None

class ChatOut(BaseModel):
    status: str
    mode_used: str
    answer: str
    steps: Any = None
    latency_ms: int

class BashRunIn(BaseModel):
    cmd: List[str] = Field(..., min_length=1)

class BashRunOut(BaseModel):
    stdout: str
    stderr: str
    code: int

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status":"ok","agent_router":AGENT_ROUTER_URL,"bash_bridge":BASH_BRIDGE_URL,"default_mode":DEFAULT_MODE,"ts":int(time.time())}

@app.on_event("startup")
async def _warmup():
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await client.post(f"{AGENT_ROUTER_URL}/agent", json={"chatInput":"PONG","mode":DEFAULT_MODE})
    except Exception:
        pass

async def call_agent_router(chat_input: str, mode: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(f"{AGENT_ROUTER_URL}/agent", json={"chatInput": chat_input, "mode": mode})
        r.raise_for_status()
        return r.json()

async def call_bash_bridge(cmd: List[str]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASH_BRIDGE_URL}/run", json={"cmd": cmd})
        r.raise_for_status()
        return r.json()

def extract_tool_call(text: str) -> Optional[Dict[str, Any]]:
    txt = (text or "").strip()
    if not txt:
        return None
    candidates = [txt] if (txt.startswith("{") and txt.endswith("}")) else []
    if not candidates:
        s, e = txt.find("{"), txt.rfind("}")
        if s != -1 and e != -1 and e > s:
            candidates.append(txt[s:e+1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and obj.get("tool") == "bash" and isinstance(obj.get("cmd"), list) and obj["cmd"]:
                if all(isinstance(x, str) for x in obj["cmd"]):
                    return obj
        except Exception:
            pass
    return None

@app.post("/bash", response_model=BashRunOut)
async def bash_run(payload: BashRunIn) -> BashRunOut:
    try:
        data = await call_bash_bridge(payload.cmd)
        return BashRunOut(stdout=data.get("stdout",""), stderr=data.get("stderr",""), code=int(data.get("code",-1)))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail={"where":"bash-bridge","status":e.response.status_code,"body":e.response.text[:1000]})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"where":"bash-bridge","error":str(e)})

@app.post("/agent", response_model=ChatOut)
async def agent(payload: ChatIn) -> ChatOut:
    mode = (payload.mode or DEFAULT_MODE).strip() or DEFAULT_MODE
    t0 = time.time()

    system_hint = 'If you need to run a shell command, respond with ONLY a JSON object like {"tool":"bash","cmd":["ls","-la"]}. Otherwise respond normally.'
    first_prompt = f"{system_hint}\n\nUSER:\n{payload.chatInput}"

    try:
        first = await call_agent_router(first_prompt, mode)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail={"where":"agent-router","status":e.response.status_code,"body":e.response.text[:1000]})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"where":"agent-router","error":str(e)})

    answer1 = (first.get("answer") or "").strip()
    tool = extract_tool_call(answer1)

    if not tool:
        return ChatOut(status=first.get("status","ok"), mode_used=first.get("mode_used",mode), answer=answer1, steps=first.get("steps"), latency_ms=int((time.time()-t0)*1000))

    try:
        tool_result = await call_bash_bridge(tool["cmd"])
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail={"where":"bash-bridge","status":e.response.status_code,"body":e.response.text[:1000]})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"where":"bash-bridge","error":str(e)})

    tool_context = {"tool":"bash","cmd":tool["cmd"],"result":tool_result}
    final_prompt = "Use the tool output to answer.\n\nUSER:\n" + payload.chatInput + "\n\nTOOL_CONTEXT:\n" + json.dumps(tool_context, ensure_ascii=False) + "\n\nANSWER:"
    try:
        final = await call_agent_router(final_prompt, mode)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail={"where":"agent-router","status":e.response.status_code,"body":e.response.text[:1000]})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"where":"agent-router","error":str(e)})

    return ChatOut(
        status=final.get("status","ok"),
        mode_used=final.get("mode_used",mode),
        answer=(final.get("answer") or "").strip(),
        steps={"first": first.get("steps"), "tool": tool_context, "final": final.get("steps")},
        latency_ms=int((time.time()-t0)*1000),
    )
