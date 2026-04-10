#!/usr/bin/env python3
"""
executor_agent.py — AI-OS Local Executor Agent
Aplica alterações aprovadas pelo reviewer. Nunca executa sem aprovação.

Usage:
  python3 agents/executor/executor_agent.py --log runtime/agent_logs/reviewer_*.json
  python3 agents/executor/executor_agent.py --diff patch.diff --decision APPROVED

Exit codes: 0=aplicado, 1=rejeitado/bloqueado, 2=erro
"""
import sys, os, json, subprocess, pathlib, datetime, shlex

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", pathlib.Path(__file__).parents[2]))
LOGS_DIR  = AIOS_ROOT / "runtime" / "agent_logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Comandos nunca permitidos (bloqueio absoluto) ─────────────────────────────
ALWAYS_DENY = [
    "rm -rf", "rm -r /", "rm -fr",
    "docker system prune", "docker volume rm", "docker network prune",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod 777", "chown -R root", "chown root",
    "mkfs", "dd if=", "fdisk",
    "> /dev/", "| sudo tee /dev/",
]

# ── Comandos seguros sem aprovação humana ─────────────────────────────────────
SAFE_PATTERNS = [
    "git diff", "git status", "git log",
    "python3 -m pytest", "python3 -c ",
    "cat ", "ls ", "echo ",
    "grep ", "find . ",
    "python3 bin/", "bash bin/",
]


def _is_always_denied(cmd: str) -> bool:
    cl = cmd.lower()
    return any(p.lower() in cl for p in ALWAYS_DENY)


def _is_safe(cmd: str) -> bool:
    return any(cmd.startswith(p) for p in SAFE_PATTERNS)


def _git_apply(diff: str) -> dict:
    """Aplica diff via git apply."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(diff)
        patch_path = f.name
    try:
        r = subprocess.run(
            ["git", "apply", "--check", patch_path],
            capture_output=True, text=True, cwd=str(AIOS_ROOT)
        )
        if r.returncode != 0:
            return {"ok": False, "error": f"git apply --check falhou: {r.stderr[:300]}"}
        r2 = subprocess.run(
            ["git", "apply", patch_path],
            capture_output=True, text=True, cwd=str(AIOS_ROOT)
        )
        if r2.returncode == 0:
            return {"ok": True, "output": "patch aplicado"}
        return {"ok": False, "error": r2.stderr[:300]}
    finally:
        pathlib.Path(patch_path).unlink(missing_ok=True)


def execute(decision: str, diff: str = "", goal: str = "",
            require_human: bool = False) -> dict:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    if decision != "APPROVED":
        result = {"ts": ts, "role": "executor", "status": "skipped",
                  "reason": f"reviewer decision={decision}", "goal": goal}
        _write_log(ts, result)
        print(f"[executor] skipped — decision={decision}")
        return result

    if require_human:
        print(f"\n[executor] APROVAÇÃO HUMANA NECESSÁRIA")
        print(f"  goal: {goal}")
        print(f"  diff: {len(diff)} chars")
        ans = input("  Aprovar? (yes/NO): ").strip().lower()
        if ans != "yes":
            result = {"ts": ts, "role": "executor", "status": "rejected_human",
                      "reason": "utilizador rejeitou", "goal": goal}
            _write_log(ts, result)
            print("[executor] rejeitado pelo utilizador")
            return result

    if not diff.strip():
        result = {"ts": ts, "role": "executor", "status": "skipped",
                  "reason": "diff vazio", "goal": goal}
        _write_log(ts, result)
        print("[executor] skipped — diff vazio")
        return result

    # Verificação final de segurança no diff
    if _is_always_denied(diff):
        result = {"ts": ts, "role": "executor", "status": "blocked",
                  "reason": "diff contém padrão ALWAYS_DENY", "goal": goal}
        _write_log(ts, result)
        print("[executor] BLOCKED — padrão destrutivo no diff")
        return result

    print(f"[executor] a aplicar diff ({len(diff)} chars)...", flush=True)
    apply_result = _git_apply(diff)

    status = "applied" if apply_result["ok"] else "error"
    result = {"ts": ts, "role": "executor", "status": status,
              "goal": goal, "apply": apply_result}
    _write_log(ts, result)

    if apply_result["ok"]:
        print("[executor] patch aplicado com sucesso")
    else:
        print(f"[executor] ERRO: {apply_result.get('error','')}")

    return result


def _write_log(ts: str, entry: dict):
    (LOGS_DIR / f"executor_{ts}.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--log" in args:
        idx = args.index("--log")
        data = json.loads(pathlib.Path(args[idx + 1]).read_text())
        decision = data.get("decision", "REJECTED")
        goal     = data.get("goal", "")
        diff     = data.get("diff", "")
        # Carregar diff do log do engineer se disponível
        if not diff:
            eng_logs = sorted(LOGS_DIR.glob("engineer_*.json"))
            if eng_logs:
                eng_data = json.loads(eng_logs[-1].read_text())
                diff = eng_data.get("diff", "")
    elif "--diff" in args and "--decision" in args:
        diff     = pathlib.Path(args[args.index("--diff") + 1]).read_text()
        decision = args[args.index("--decision") + 1]
        goal     = args[args.index("--goal") + 1] if "--goal" in args else ""
    else:
        print("Usage: executor_agent.py --log reviewer_*.json")
        print("       executor_agent.py --diff patch.diff --decision APPROVED [--goal '...']")
        sys.exit(1)

    out = execute(decision, diff, goal)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0 if out["status"] in ("applied", "skipped") else 1)
