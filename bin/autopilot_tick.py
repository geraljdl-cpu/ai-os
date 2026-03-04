#!/usr/bin/env python3
"""
autopilot_tick.py — orquestrador do ciclo completo autopilot (Ponto 4).

Pipeline:
  peek task (PG) → branch → agent /think → apply patch → gates
  → reflect loop (max N) → commit + merge → update PG

Chamado pelo aios-autopilot.service (oneshot, 30s timer).
"""
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
JOBS_DIR  = AIOS_ROOT / "runtime" / "jobs"
LOCK_FILE = AIOS_ROOT / "runtime" / "autopilot_tick.lock"

AGENT_URL   = os.environ.get("AIOS_AGENT_URL",    "http://127.0.0.1:8010")
AGENT_MODE  = os.environ.get("AIOS_AGENT_MODE",   "openai")
MAX_REFLECT = int(os.environ.get("AIOS_MAX_REFLECT", "2"))

sys.path.insert(0, str(AIOS_ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# Shell helper
# ──────────────────────────────────────────────────────────────────────────────

def sh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True, cwd=str(AIOS_ROOT))
    if check and r.returncode != 0:
        raise RuntimeError(
            f"cmd failed rc={r.returncode}\n"
            f"CMD: {cmd}\n"
            f"STDOUT: {r.stdout[:800]}\n"
            f"STDERR: {r.stderr[:800]}"
        )
    return r


def log(msg: str, job_log: pathlib.Path | None = None) -> None:
    line = f"[tick] {msg}"
    print(line, flush=True)
    if job_log:
        with job_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Postgres
# ──────────────────────────────────────────────────────────────────────────────

def pg_peek() -> dict | None:
    from bin import backlog_pg
    # task_type=None → devolve qualquer tipo (DEV/OPS/RESEARCH)
    return backlog_pg.peek_next_task_json(task_type=None)


def pg_mark(task_id: str, status: str, last_error: str | None = None) -> None:
    from bin import backlog_pg
    backlog_pg.update_task(task_id, status=status, last_error=last_error)


# ──────────────────────────────────────────────────────────────────────────────
# Git
# ──────────────────────────────────────────────────────────────────────────────

def git_prepare_branch(branch: str) -> None:
    sh("git checkout main")
    sh("git pull --ff-only 2>/dev/null || true", check=False)
    sh(f"git checkout -b {branch}")


def git_abort_and_reset(patch_files: list[str]) -> None:
    """Reseta apenas os ficheiros tocados pelo patch. Não usa git clean."""
    for f in patch_files:
        p = AIOS_ROOT / f
        # Se o ficheiro não estava no index (criado pelo patch) → apaga
        r = sh(f"git ls-files --error-unmatch {f}", check=False)
        if r.returncode != 0:
            if p.exists():
                p.unlink()
        else:
            sh(f"git checkout -- {f}", check=False)


def git_commit(message: str) -> None:
    sh("git add -A")
    r = sh(f'git commit -m "{message}"', check=False)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        raise RuntimeError(f"git commit falhou:\n{r.stderr[:400]}")


# ──────────────────────────────────────────────────────────────────────────────
# Agent + patch engine (formato search/replace)
# ──────────────────────────────────────────────────────────────────────────────

_SR_BLOCK_RE = re.compile(
    r"<<<<\s*FILE:\s*(?P<file>[^\n]+)\n"
    r"SEARCH:\n(?P<search>.*?)\n====\n"
    r"REPLACE:\n(?P<replace>.*?)\n"
    r">>>>",
    re.DOTALL,
)
_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\n(.*?)```", re.DOTALL)


def _collect_file_context(goal: str, max_lines: int = 300) -> str:
    candidates = re.findall(r"[\w./\-]+\.(?:js|py|sh|json|html|css|ts|md)", goal)
    sections: list[str] = []
    seen: set[pathlib.Path] = set()
    for cand in candidates:
        for base in [
            AIOS_ROOT / cand,
            AIOS_ROOT / "ui"  / pathlib.Path(cand).name,
            AIOS_ROOT / "bin" / pathlib.Path(cand).name,
        ]:
            if base in seen or not base.is_file():
                continue
            seen.add(base)
            try:
                lines = base.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = base.relative_to(AIOS_ROOT)
            if len(lines) <= max_lines:
                content = "\n".join(lines)
            else:
                content = "\n".join(lines[:150]) + f"\n\n... [{len(lines)-250} linhas omitidas] ...\n\n" + "\n".join(lines[-100:])
            sections.append(f"=== {rel} ({len(lines)} linhas) ===\n{content}")
    return "\n\n".join(sections)


def _resolve_file_path(rel: str) -> pathlib.Path | None:
    """Tenta resolver path relativo com variações comuns (com/sem bin/, ui/)."""
    candidates = [
        AIOS_ROOT / rel,
        AIOS_ROOT / "bin" / pathlib.Path(rel).name,
        AIOS_ROOT / "ui"  / pathlib.Path(rel).name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _apply_search_replace(raw: str) -> list[str]:
    """Aplica blocos <<<< FILE. Devolve ficheiros alterados ou [] se sem blocos."""
    blocks = list(_SR_BLOCK_RE.finditer(raw))
    if not blocks:
        return []
    changed: list[str] = []
    for m in blocks:
        rel  = m.group("file").strip()
        srch = m.group("search")
        repl = m.group("replace")
        tgt  = _resolve_file_path(rel)
        if tgt is None:
            if srch.strip():
                raise RuntimeError(f"search/replace: ficheiro não existe: {rel}")
            # ficheiro novo — usa path tal qual
            tgt = AIOS_ROOT / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(repl, encoding="utf-8")
            changed.append(str(tgt.relative_to(AIOS_ROOT)))
            continue
        orig = tgt.read_text(encoding="utf-8", errors="replace")
        if srch not in orig:
            raise RuntimeError(
                f"search/replace: padrão não encontrado em {tgt.relative_to(AIOS_ROOT)}\n"
                f"SEARCH[:200]: {srch[:200]!r}"
            )
        tgt.write_text(orig.replace(srch, repl, 1), encoding="utf-8")
        changed.append(str(tgt.relative_to(AIOS_ROOT)))
    return changed


def _apply_unified_diff(diff_text: str, patch_path: pathlib.Path) -> list[str]:
    patch_path.write_text(diff_text, encoding="utf-8")
    r = sh(f"git apply --check {patch_path}", check=False)
    if r.returncode == 0:
        sh(f"git apply {patch_path}")
    else:
        r3 = sh(f"git apply --3way {patch_path}", check=False)
        if r3.returncode != 0:
            raise RuntimeError(
                f"git apply falhou:\nCHECK: {r.stderr[:400]}\n3WAY: {r3.stderr[:400]}"
            )
    files = [m.group(1) for line in diff_text.splitlines()
             if (m := re.match(r"^\+\+\+ b/(.+)$", line))]
    return files


def _raw_agent_call(prompt: str) -> str:
    payload = json.dumps({"prompt": prompt, "mode": AGENT_MODE})
    r = subprocess.run(
        ["curl", "-fsS", "--max-time", "90",
         f"{AGENT_URL}/think",
         "-H", "Content-Type: application/json",
         "-d", payload],
        text=True, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"agent /think unreachable: {r.stderr[:300]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"agent resposta inválida: {r.stdout[:300]}")
    text = data.get("text") or data.get("output") or data.get("result") or ""
    if not text:
        raise RuntimeError(f"agent devolveu campo vazio: {r.stdout[:300]}")
    return text


def agent_call_and_apply(prompt: str, patch_path: pathlib.Path) -> tuple[str, list[str]]:
    """Chama agente, aplica patch. Devolve (raw, ficheiros_alterados)."""
    raw = _raw_agent_call(prompt)

    # 1. search/replace (preferido)
    changed = _apply_search_replace(raw)
    if changed:
        return raw, changed

    # 2. unified diff fallback
    m = _DIFF_FENCE_RE.search(raw)
    diff_text = m.group(1).strip() if m else ""
    if not diff_text:
        for i, line in enumerate(raw.splitlines()):
            if line.startswith("---") or line.startswith("diff --git"):
                diff_text = "\n".join(raw.splitlines()[i:]).strip()
                break
    if diff_text:
        changed = _apply_unified_diff(diff_text, patch_path)
        return raw, changed

    # NOOP
    if "NOOP" in raw.upper():
        return raw, []

    raise RuntimeError("agent não gerou search/replace nem diff aplicável")


def build_patch_prompt(goal: str) -> str:
    ctx = _collect_file_context(goal)
    ctx_block = f"\n\nFICHEIROS ACTUAIS:\n{ctx}" if ctx else ""
    return (
        "ROLE: SENIOR DEVELOPER\n"
        f"TAREFA: {goal}\n\n"
        "REGRAS ESTRITAS:\n"
        "- Usa EXCLUSIVAMENTE o formato search/replace abaixo.\n"
        "- Para cada ficheiro a modificar:\n\n"
        "<<<< FILE: <path relativo, ex: ui/server.js>\n"
        "SEARCH:\n"
        "<texto EXACTO a substituir>\n"
        "====\n"
        "REPLACE:\n"
        "<novo texto>\n"
        ">>>>\n\n"
        "- Para ficheiro novo: SEARCH vazio.\n"
        "- Nada fora dos blocos <<<< >>>>.\n"
        "- Sem alterações: devolve NOOP."
        f"{ctx_block}"
    )


def build_reflect_prompt(goal: str, gate_log: str, files: list[str]) -> str:
    tmpl = (AIOS_ROOT / "bin" / "reflect_prompt.txt").read_text(encoding="utf-8")
    return (
        tmpl
        .replace("{{GOAL}}", goal)
        .replace("{{GATE_LOG}}", gate_log[-4000:])
        .replace("{{FILES}}", "\n".join(files) or "(none)")
    )


# ──────────────────────────────────────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────────────────────────────────────

def run_gates(job_dir: pathlib.Path) -> bool:
    r = subprocess.run(
        [str(AIOS_ROOT / "bin" / "gates.sh"), str(job_dir)],
        text=True, capture_output=True, cwd=str(AIOS_ROOT),
    )
    return r.returncode == 0


def read_gate_log(job_dir: pathlib.Path) -> str:
    p = job_dir / "gate.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


# ──────────────────────────────────────────────────────────────────────────────
# Main cycle
# ──────────────────────────────────────────────────────────────────────────────

def make_job_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def run_cycle() -> str:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    task = pg_peek()
    if not task:
        print("[tick] IDLE — no pending tasks")
        return "IDLE"

    task_id = task.get("id") or ""
    goal    = (task.get("goal") or task.get("title") or "").strip()

    if not task_id or not goal:
        if task_id:
            pg_mark(task_id, "failed", last_error="goal vazio")
        return "ERROR"

    job_id  = make_job_id()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    jlog    = job_dir / "tick.log"

    (job_dir / "goal.txt").write_text(goal, encoding="utf-8")
    (job_dir / "task.json").write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

    branch = f"aios/{job_id}"
    log(f"job={job_id} task={task_id} branch={branch}", jlog)
    log(f"goal={goal[:120]}", jlog)

    patch_files: list[str] = []

    try:
        pg_mark(task_id, "running")
        git_prepare_branch(branch)
        log("branch criada", jlog)

        # Gera e aplica patch
        log("a chamar agent /think ...", jlog)
        prompt = build_patch_prompt(goal)
        (job_dir / "agent_request.txt").write_text(prompt, encoding="utf-8")

        patch_path = job_dir / "patch.diff"
        raw, patch_files = agent_call_and_apply(prompt, patch_path)
        (job_dir / "agent_response.json").write_text(
            json.dumps({"text": raw}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (job_dir / "changed_files.txt").write_text("\n".join(patch_files), encoding="utf-8")
        log(f"patch aplicado: {patch_files}", jlog)

        # Gates + reflexão
        ok = run_gates(job_dir)
        rounds = 0

        while not ok and rounds < MAX_REFLECT:
            rounds += 1
            gate_log_txt = read_gate_log(job_dir)
            log(f"gates falhou — reflect round {rounds}/{MAX_REFLECT}", jlog)
            (job_dir / f"reflect_round_{rounds}.txt").write_text(gate_log_txt, encoding="utf-8", errors="replace")

            prev_files = patch_files[:]   # guarda antes de reset
            git_abort_and_reset(patch_files)
            patch_files = []

            reflect_prompt = build_reflect_prompt(goal, gate_log_txt, prev_files)
            (job_dir / f"reflect_prompt_{rounds}.txt").write_text(reflect_prompt, encoding="utf-8")

            inc_path = job_dir / f"patch_reflect_{rounds}.diff"
            inc_raw, patch_files = agent_call_and_apply(reflect_prompt, inc_path)
            inc_path.write_text(inc_raw, encoding="utf-8")
            (job_dir / "changed_files.txt").write_text("\n".join(patch_files), encoding="utf-8")
            log(f"reflect_{rounds} aplicado: {patch_files}", jlog)

            ok = run_gates(job_dir)

        if not ok:
            gate_log_txt = read_gate_log(job_dir)
            log(f"FAILED após {rounds} reflect(s)", jlog)
            (job_dir / "final.json").write_text(
                json.dumps({"ok": False, "reason": "gates_failed", "job_id": job_id, "rounds": rounds}, indent=2),
                encoding="utf-8"
            )
            pg_mark(task_id, "failed", last_error=gate_log_txt[-400:])
            git_abort_and_reset(patch_files)
            sh(f"git checkout main && git branch -D {branch}", check=False)
            return "FAILED_GATES"

        # Commit + Merge
        log("gates OK — commit + merge ...", jlog)
        git_commit(f"aios: autopilot {job_id} — {goal[:60]}")

        merge_r = subprocess.run(
            [str(AIOS_ROOT / "bin" / "merge_if_clean.sh"), branch],
            text=True, capture_output=True, cwd=str(AIOS_ROOT),
        )
        merge_out = (merge_r.stdout + merge_r.stderr).strip()
        log(f"merge: {merge_out}", jlog)

        if "MERGE_OK" not in merge_out:
            raise RuntimeError(f"merge falhou: {merge_out}")

        (job_dir / "final.json").write_text(
            json.dumps({"ok": True, "job_id": job_id, "branch": branch, "rounds": rounds}, indent=2),
            encoding="utf-8"
        )
        pg_mark(task_id, "done")
        log("DONE_MERGED", jlog)
        return "DONE_MERGED"

    except Exception as exc:
        err_msg = str(exc)
        log(f"ERROR: {err_msg[:400]}", jlog)
        try:
            git_abort_and_reset(patch_files)
            sh("git checkout main 2>/dev/null || true", check=False)
            sh(f"git branch -D {branch} 2>/dev/null || true", check=False)
        except Exception:
            pass
        try:
            pg_mark(task_id, "failed", last_error=err_msg[:400])
        except Exception:
            pass
        (job_dir / "final.json").write_text(
            json.dumps({"ok": False, "job_id": job_id, "error": err_msg[:800]}, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return "ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# Entry point com flock
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[tick] já em execução — exit")
        sys.exit(0)
    try:
        outcome = run_cycle()
        print(f"[tick] outcome={outcome}")
        sys.exit(0 if outcome in ("IDLE", "DONE_MERGED") else 1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
