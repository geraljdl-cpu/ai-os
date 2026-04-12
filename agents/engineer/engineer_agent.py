#!/usr/bin/env python3
"""
engineer_agent.py — AI-OS Local Engineer Agent
Usa Aider + qwen2.5-coder:14b (nodegpu) para propor alterações de código.

Usage:
  python3 agents/engineer/engineer_agent.py "adicionar type hints a bin/foo.py"
  python3 agents/engineer/engineer_agent.py "fix bug X" bin/foo.py bin/bar.py

Output:
  - diff em stdout
  - log em runtime/agent_logs/engineer_YYYYMMDD_HHMMSS.jsonl
"""
import sys, os, json, subprocess, pathlib, datetime, tempfile

AIOS_ROOT  = pathlib.Path(os.environ.get("AIOS_ROOT", pathlib.Path(__file__).parents[2]))
LOGS_DIR   = AIOS_ROOT / "runtime" / "agent_logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

_CFG_FILE  = AIOS_ROOT / "config" / "local_ai.json"
_CFG       = json.loads(_CFG_FILE.read_text()) if _CFG_FILE.exists() else {}
_PIPELINE  = _CFG.get("agent_pipeline", {})

OLLAMA_URL = os.environ.get("OLLAMA_URL", _PIPELINE.get("ollama_url", "http://192.168.1.202:11434"))
MODEL      = os.environ.get("ENGINEER_MODEL", _PIPELINE.get("engineer_model", "qwen2.5-coder:14b"))
AIDER_BIN  = os.environ.get("AIDER_BIN", "aider")


def run(goal: str, files: list[str] = None) -> dict:
    ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    files = files or []

    # Capturar diff antes e depois
    diff_before = _git_diff()

    cmd = [
        AIDER_BIN,
        "--model", f"ollama/{MODEL}",
        "--no-auto-commits",
        "--yes",
        "--message", goal,
    ] + files

    env = os.environ.copy()
    env["OLLAMA_API_BASE"] = OLLAMA_URL

    print(f"[engineer] model={MODEL}  goal={goal[:80]}", flush=True)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True,
                            cwd=str(AIOS_ROOT), timeout=300)

    diff_after = _git_diff()
    proposed   = _diff_delta(diff_before, diff_after)

    log_entry = {
        "ts":        ts,
        "role":      "engineer",
        "goal":      goal,
        "files":     files,
        "model":     MODEL,
        "exit_code": result.returncode,
        "stdout":    result.stdout[-3000:],
        "stderr":    result.stderr[-1000:],
        "diff":      proposed,
        "status":    "ok" if result.returncode == 0 else "error",
    }
    _write_log(ts, log_entry)

    if proposed:
        print("[engineer] diff proposto:")
        print(proposed[:2000])
    else:
        print("[engineer] sem alterações propostas")
        print(result.stdout[-1000:])

    return log_entry


def _git_diff() -> str:
    try:
        r = subprocess.run(["git", "diff"], capture_output=True, text=True,
                           cwd=str(AIOS_ROOT))
        return r.stdout
    except Exception:
        return ""


def _diff_delta(before: str, after: str) -> str:
    """Retorna apenas as linhas novas no diff (delta)."""
    if after and after != before:
        return after
    return ""


def _write_log(ts: str, entry: dict):
    log_file = LOGS_DIR / f"engineer_{ts}.json"
    log_file.write_text(json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: engineer_agent.py '<goal>' [file1 file2 ...]")
        sys.exit(1)
    goal  = sys.argv[1]
    files = sys.argv[2:]
    out   = run(goal, files)
    sys.exit(0 if out["status"] == "ok" else 1)
