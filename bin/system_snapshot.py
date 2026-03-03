#!/usr/bin/env python3
"""
AI-OS System Snapshot
Gera runtime/system_snapshot.json com estado actual do sistema.
Usa apenas python3 stdlib: pathlib, os, json, datetime.
"""
import os, json, datetime, pathlib, importlib.util

AIOS_ROOT   = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
JOBS_DIR    = AIOS_ROOT / "runtime" / "jobs"
OUT_FILE    = AIOS_ROOT / "runtime" / "system_snapshot.json"


def _load_backlog_pg():
    spec = importlib.util.spec_from_file_location("backlog_pg", AIOS_ROOT / "bin" / "backlog_pg.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dir_size_bytes(p: pathlib.Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _last_n_jobs(n: int = 5) -> list:
    """Devolve os N jobs mais recentes (por mtime) com id e ficheiros presentes."""
    if not JOBS_DIR.exists():
        return []
    entries = []
    for p in JOBS_DIR.iterdir():
        if not p.is_dir():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        files = [f.name for f in p.iterdir() if f.is_file()] if p.is_dir() else []
        entries.append({"id": p.name, "mtime": mtime, "files": files})
    entries.sort(key=lambda x: x["mtime"], reverse=True)
    result = []
    for e in entries[:n]:
        result.append({
            "id":    e["id"],
            "mtime": datetime.datetime.utcfromtimestamp(e["mtime"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": e["files"],
        })
    return result


def generate() -> dict:
    now = datetime.datetime.utcnow()

    # Contagens do backlog
    counts = {"pending": 0, "done": 0, "failed": 0, "skipped": 0, "running": 0, "other": 0}
    try:
        bp = _load_backlog_pg()
        tasks = bp.list_tasks()
        for t in tasks:
            s = t.get("status", "other")
            if s in counts:
                counts[s] += 1
            else:
                counts["other"] += 1
    except Exception as e:
        counts["_error"] = str(e)

    # Tamanho da pasta runtime
    runtime_dir   = AIOS_ROOT / "runtime"
    runtime_bytes = _dir_size_bytes(runtime_dir)

    snapshot = {
        "generated_at":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aios_root":      str(AIOS_ROOT),
        "backlog_counts": counts,
        "last_5_jobs":    _last_n_jobs(5),
        "runtime_size":   {
            "bytes": runtime_bytes,
            "mb":    round(runtime_bytes / 1_048_576, 2),
        },
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return snapshot


if __name__ == "__main__":
    s = generate()
    print(json.dumps(s, indent=2, ensure_ascii=False))
