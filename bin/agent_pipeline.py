#!/usr/bin/env python3
"""
agent_pipeline.py — AI-OS Local Agent Pipeline
Orquestra: task → engineer → reviewer → executor

Usage:
  python3 bin/agent_pipeline.py "adicionar type hints a bin/foo.py"
  python3 bin/agent_pipeline.py --auto "fix: corrigir import em bin/bar.py" bin/bar.py
  python3 bin/agent_pipeline.py --dry-run "descrever alteração"

Flags:
  --auto        executa sem pausa interactiva (se reviewer aprovar)
  --dry-run     engineer propõe, reviewer revê, executor não aplica
  --model M     override do modelo engineer (default: qwen2.5-coder:14b)
  --files f1 f2 ficheiros a passar ao engineer
"""
import sys, os, json, pathlib, datetime, argparse

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(AIOS_ROOT / "agents" / "engineer"))
sys.path.insert(0, str(AIOS_ROOT / "agents" / "reviewer"))
sys.path.insert(0, str(AIOS_ROOT / "agents" / "executor"))

import engineer_agent
import reviewer_agent
import executor_agent

MEMORY_DIR = AIOS_ROOT / "runtime" / "agent_memory"
LOGS_DIR   = AIOS_ROOT / "runtime" / "agent_logs"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

MEMORY_FILE = MEMORY_DIR / "pipeline_history.jsonl"


def _append_memory(entry: dict):
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _recent_memory(n: int = 5) -> list:
    if not MEMORY_FILE.exists():
        return []
    lines = MEMORY_FILE.read_text().splitlines()
    return [json.loads(l) for l in lines[-n:] if l.strip()]


def run_pipeline(goal: str, files: list = None, auto: bool = False,
                 dry_run: bool = False, model: str = None) -> dict:
    ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    files = files or []

    print(f"\n{'='*60}")
    print(f"[pipeline] {ts}")
    print(f"[pipeline] goal: {goal}")
    print(f"[pipeline] auto={auto}  dry_run={dry_run}  files={files}")
    print(f"{'='*60}\n")

    # Contexto de memória recente
    recent = _recent_memory(3)
    context = ""
    if recent:
        context = "Tarefas recentes: " + "; ".join(
            f"{r.get('goal','?')[:50]}→{r.get('executor_status','?')}"
            for r in recent
        )

    # ── 1. ENGINEER ────────────────────────────────────────────────────────────
    if model:
        os.environ["ENGINEER_MODEL"] = model

    eng_result = engineer_agent.run(goal, files)

    if eng_result["status"] != "ok":
        summary = {"ts": ts, "goal": goal, "stage": "engineer",
                   "outcome": "error", "executor_status": "skipped"}
        _append_memory(summary)
        print(f"\n[pipeline] FALHOU na fase engineer: {eng_result.get('stderr','')[:200]}")
        return summary

    diff = eng_result.get("diff", "")

    # ── 2. REVIEWER ────────────────────────────────────────────────────────────
    rev_result = reviewer_agent.review(goal, diff, context)
    decision   = rev_result.get("decision", "REJECTED")

    if decision == "REJECTED":
        summary = {"ts": ts, "goal": goal, "stage": "reviewer",
                   "outcome": "rejected", "reason": rev_result.get("reason",""),
                   "executor_status": "skipped"}
        _append_memory(summary)
        print(f"\n[pipeline] REJEITADO pelo reviewer: {rev_result.get('reason','')[:200]}")
        return summary

    if decision == "NEEDS_REVISION":
        summary = {"ts": ts, "goal": goal, "stage": "reviewer",
                   "outcome": "needs_revision", "reason": rev_result.get("reason",""),
                   "executor_status": "skipped"}
        _append_memory(summary)
        print(f"\n[pipeline] NEEDS_REVISION: {rev_result.get('reason','')[:200]}")
        print("[pipeline] Corrige manualmente e corre novamente.")
        return summary

    # ── 3. EXECUTOR ────────────────────────────────────────────────────────────
    if dry_run:
        summary = {"ts": ts, "goal": goal, "stage": "executor",
                   "outcome": "dry_run", "executor_status": "dry_run",
                   "decision": decision}
        _append_memory(summary)
        print("\n[pipeline] DRY-RUN — diff não aplicado (reviewer APPROVED)")
        return summary

    require_human = not auto
    exec_result   = executor_agent.execute(decision, diff, goal,
                                            require_human=require_human)
    exec_status   = exec_result.get("status", "error")

    summary = {
        "ts": ts, "goal": goal, "stage": "executor",
        "outcome": "done", "executor_status": exec_status,
        "decision": decision,
        "risks": rev_result.get("risks", []),
    }
    _append_memory(summary)

    print(f"\n[pipeline] concluído — executor: {exec_status}")
    return summary


def main():
    p = argparse.ArgumentParser(description="AI-OS Agent Pipeline")
    p.add_argument("goal", help="Objectivo da tarefa")
    p.add_argument("--auto",     action="store_true", help="Não pedir aprovação humana")
    p.add_argument("--dry-run",  action="store_true", help="Propor mas não aplicar")
    p.add_argument("--model",    default=None,        help="Override modelo engineer")
    p.add_argument("--files",    nargs="*", default=[], help="Ficheiros alvo")
    args = p.parse_args()

    result = run_pipeline(
        goal=args.goal,
        files=args.files,
        auto=args.auto,
        dry_run=args.dry_run,
        model=args.model,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result.get("outcome") in ("done", "dry_run") else 1)


if __name__ == "__main__":
    main()
