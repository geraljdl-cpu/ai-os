#!/usr/bin/env python3
"""
reviewer_agent.py — AI-OS Local Reviewer Agent
Valida diff proposto pelo engineer: segurança, lógica, coerência.

Usage:
  python3 agents/reviewer/reviewer_agent.py '<goal>' '<diff>'
  python3 agents/reviewer/reviewer_agent.py --log runtime/agent_logs/engineer_*.json

Output:
  JSON com decision: APPROVED | REJECTED | NEEDS_REVISION
  Exit 0 = APPROVED, Exit 1 = REJECTED/NEEDS_REVISION
"""
import sys, os, json, re, pathlib, datetime, urllib.request

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", pathlib.Path(__file__).parents[2]))
LOGS_DIR  = AIOS_ROOT / "runtime" / "agent_logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

_CFG_FILE  = AIOS_ROOT / "config" / "local_ai.json"
_CFG       = json.loads(_CFG_FILE.read_text()) if _CFG_FILE.exists() else {}
_PIPELINE  = _CFG.get("agent_pipeline", {})

OLLAMA_URL = os.environ.get("OLLAMA_URL", _PIPELINE.get("ollama_url", "http://192.168.1.120:11434"))
MODEL      = os.environ.get("REVIEWER_MODEL", _PIPELINE.get("reviewer_model", "qwen2.5:14b"))

# Padrões sempre rejeitados sem consultar LLM
DENY_PATTERNS = [
    "rm -rf", "rm -r /", "docker system prune", "docker volume rm",
    "shutdown", "reboot", "chmod 777", "chown -R root",
    "DROP TABLE", "DROP DATABASE", "TRUNCATE",
    "os.system(", "eval(", "exec(",
]


def _static_safety_check(diff: str) -> tuple:
    for pattern in DENY_PATTERNS:
        if pattern.lower() in diff.lower():
            return False, f"DENY_PATTERN: '{pattern}'"
    return True, ""


def _call_ollama(prompt: str) -> str:
    body = json.dumps({
        "model": MODEL, "prompt": prompt,
        "stream": False, "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()).get("response", "")
    except Exception as e:
        return f"ERROR: {e}"


def _parse_decision(raw: str) -> dict:
    m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    up = raw.upper()
    if "APPROVED" in up:
        return {"decision": "APPROVED", "reason": raw[:200], "risks": []}
    if "NEEDS_REVISION" in up:
        return {"decision": "NEEDS_REVISION", "reason": raw[:200], "risks": []}
    return {"decision": "REJECTED", "reason": raw[:200], "risks": []}


def review(goal: str, diff: str, context: str = "") -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    safe, deny_reason = _static_safety_check(diff)
    if not safe:
        result = {"ts": ts, "role": "reviewer", "decision": "REJECTED",
                  "reason": deny_reason, "model": "static_check",
                  "goal": goal, "diff_len": len(diff), "risks": []}
        _write_log(ts, result)
        print(f"[reviewer] REJECTED (static): {deny_reason}")
        return result

    if not diff.strip():
        result = {"ts": ts, "role": "reviewer", "decision": "REJECTED",
                  "reason": "diff vazio — sem alterações propostas",
                  "model": "static_check", "goal": goal, "diff_len": 0, "risks": []}
        _write_log(ts, result)
        print("[reviewer] REJECTED: diff vazio")
        return result

    prompt = (
        f"Revê esta alteração de código e decide se deve ser aprovada.\n\n"
        f"OBJETIVO: {goal}\n\n"
        f"DIFF:\n{diff[:3000]}\n\n"
        + (f"CONTEXTO:\n{context[:400]}\n\n" if context else "")
        + 'Responde APENAS com JSON: {"decision": "APPROVED"|"REJECTED"|"NEEDS_REVISION", '
          '"reason": "explicação concisa", "risks": ["risco1"]}\n'
          "Rejeita se: operações destrutivas, lógica errada, segurança comprometida."
    )

    print(f"[reviewer] model={MODEL}  diff={len(diff)} chars...", flush=True)
    raw  = _call_ollama(prompt)
    data = _parse_decision(raw)
    data.update({"ts": ts, "role": "reviewer", "goal": goal,
                  "model": MODEL, "diff_len": len(diff)})
    _write_log(ts, data)
    print(f"[reviewer] {data['decision']}: {str(data.get('reason',''))[:120]}")
    return data


def _write_log(ts: str, entry: dict):
    (LOGS_DIR / f"reviewer_{ts}.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--log" in sys.argv:
        idx = sys.argv.index("--log")
        data = json.loads(pathlib.Path(sys.argv[idx + 1]).read_text())
        goal, diff = data.get("goal", ""), data.get("diff", "")
    elif len(sys.argv) >= 3:
        goal, diff = sys.argv[1], sys.argv[2]
    else:
        print("Usage: reviewer_agent.py '<goal>' '<diff>'")
        print("       reviewer_agent.py --log runtime/agent_logs/engineer_*.json")
        sys.exit(1)

    out = review(goal, diff)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0 if out["decision"] == "APPROVED" else 1)
