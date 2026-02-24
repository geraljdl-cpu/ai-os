import os, json, time, subprocess, uuid
from pathlib import Path

RUNTIME = Path(os.environ.get("AIOS_RUNTIME", "/app/runtime"))
JOBS = RUNTIME / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)

def _run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr

def new_job(payload: dict) -> dict:
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "request.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    repo = Path(payload["repo_path"]).resolve()
    base = payload.get("base_branch","main")
    req = payload["request"].strip()
    branch = f"aios/{job_id}"

    log = []
    def logline(s: str):
        log.append(s)
        (job_dir / "log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")

    rc, out, err = _run(["git","rev-parse","--is-inside-work-tree"], cwd=repo)
    logline(f"git_check rc={rc}\n{out}\n{err}")
    if rc != 0:
        return {"ok": False, "job_id": job_id, "error": "not a git repo", "job_dir": str(job_dir)}

    for cmd in (["git","fetch","--all"], ["git","checkout", base], ["git","pull","--ff-only"], ["git","checkout","-b", branch]):
        rc, out, err = _run(cmd, cwd=repo)
        logline(f"{cmd} rc={rc}\n{out}\n{err}")
        if rc != 0:
            return {"ok": False, "job_id": job_id, "error": f"git failed: {cmd}", "job_dir": str(job_dir), "branch": branch}

    target = repo / "CHANGELOG_DEV.md"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    content = (
        "# AI-OS Dev Jobs\n\n"
        f"## {stamp}\n"
        "Request:\n"
        f"{req}\n\n"
        "Job:\n"
        f"{job_id}\n"
    )
    target.write_text(content, encoding="utf-8")
    logline(f"wrote {target}")

    rc, out, err = _run(["git","add","-A"], cwd=repo)
    logline(f"git add rc={rc}\n{out}\n{err}")
    rc, out, err = _run(["git","commit","-m", f"aios job {job_id}: seed changes"], cwd=repo)
    logline(f"git commit rc={rc}\n{out}\n{err}")
    if rc != 0:
        logline("commit failed (maybe no changes). continuing.")

    (job_dir / "result.json").write_text(json.dumps({
        "ok": True, "job_id": job_id, "branch": branch, "repo_path": str(repo),
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {"ok": True, "job_id": job_id, "branch": branch, "job_dir": str(job_dir)}

def list_jobs(limit=20) -> dict:
    items = []
    for p in sorted(JOBS.glob("*"), reverse=True)[:limit]:
        r = p / "result.json"
        items.append({"job_id": p.name, "ok": json.loads(r.read_text(encoding="utf-8")).get("ok") if r.exists() else None})
    return {"ok": True, "jobs": items}
