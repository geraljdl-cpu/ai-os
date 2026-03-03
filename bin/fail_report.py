#!/usr/bin/env python3
"""
AI-OS Fail Report
Gera runtime/fail_report.md com as últimas 20 tasks failed.
Usa apenas python3 stdlib: pathlib, os, json, datetime, importlib.
"""
import os, json, datetime, pathlib, importlib.util

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
OUT_FILE  = AIOS_ROOT / "runtime" / "fail_report.md"


def _load_backlog_pg():
    spec = importlib.util.spec_from_file_location("backlog_pg", AIOS_ROOT / "bin" / "backlog_pg.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def generate() -> str:
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        bp    = _load_backlog_pg()
        tasks = bp.list_tasks()
    except Exception as e:
        lines = [f"# AI-OS Fail Report\n\nErro ao carregar backlog: {e}\n"]
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUT_FILE.write_text("".join(lines))
        return "".join(lines)

    failed = [t for t in tasks if t.get("status") == "failed"]
    # ordena por updated_at desc, limita a 20
    failed.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    failed = failed[:20]

    lines = [
        f"# AI-OS — Fail Report\n\n",
        f"**Gerado:** {now}  \n",
        f"**Total failed:** {len([t for t in tasks if t.get('status') == 'failed'])}  \n",
        f"**Mostrados:** {len(failed)}\n\n",
        "---\n\n",
    ]

    if not failed:
        lines.append("_Nenhuma task failed encontrada._\n")
    else:
        lines.append("| # | ID | Título | Último erro | Data |\n")
        lines.append("|---|-----|--------|------------|------|\n")
        for i, t in enumerate(failed, 1):
            tid   = t.get("id", "?")[:12]
            title = (t.get("title") or t.get("goal", "?"))[:50].replace("|", "\\|")
            err   = (t.get("last_error") or "—")[:80].replace("|", "\\|").replace("\n", " ")
            ts    = _fmt_ts(t.get("updated_at"))
            lines.append(f"| {i} | `{tid}` | {title} | {err} | {ts} |\n")

    report = "".join(lines)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(report, encoding="utf-8")
    return report


if __name__ == "__main__":
    print(generate())
