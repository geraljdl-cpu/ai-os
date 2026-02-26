import os, json, time, subprocess, uuid, re, urllib.request
from pathlib import Path

RUNTIME = Path(os.environ.get("AIOS_RUNTIME", "/app/runtime"))
JOBS = RUNTIME / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)

AIOS_AGENT_URL = os.environ.get("AIOS_AGENT_URL", "http://agent-router:5679/agent")
AIOS_AGENT_MODE = os.environ.get("AIOS_AGENT_MODE", "openai")
AIOS_AGENT_TIMEOUT = float(os.environ.get("AIOS_AGENT_TIMEOUT", "180"))

def _run(cmd, cwd=None, input_text=None):
    p = subprocess.run(cmd, cwd=cwd, text=True, input=input_text, capture_output=True)
    return p.returncode, p.stdout, p.stderr

def _bash(script: str, cwd=None, input_text=None):
    return _run(["bash","-lc", script], cwd=cwd, input_text=input_text)

def _http_post_json(url: str, payload: dict, timeout: float):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json", "X-AIOS-TOKEN": os.environ.get("AIOS_TOKEN","")})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8","replace"))

def _extract_diff(text: str) -> str:
    m = re.search(r"```diff\s*(.*?)```", text, flags=re.S)
    if m:
        return m.group(1).strip() + "\n"
    m = re.search(r"(diff --git .*|\*\*\* Begin Patch.*)", text, flags=re.S)
    if m:
        return text[m.start():].strip() + "\n"
    return ""

def new_job(payload: dict) -> dict:
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, txt: str):
        (job_dir / name).write_text(txt, encoding="utf-8")

    log = []
    def logline(s: str):
        log.append(s)
        write("log.txt", "\n".join(log) + "\n")

    try:
        write("request.json", json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

        repo = Path(payload["repo_path"]).resolve()
        base = payload.get("base_branch","main")
        req = payload["request"].strip()
        branch = f"aios/{job_id}"
        test_cmd = payload.get("test_cmd") or "python -m compileall -q ."

        rc, out, err = _run(["git","rev-parse","--is-inside-work-tree"], cwd=repo)
        logline(f"git_check rc={rc}\n{out}\n{err}")
        if rc != 0:
            return {"ok": False, "job_id": job_id, "error": "not a git repo (or unsafe)", "job_dir": str(job_dir)}

        rc, out, err = _run(["git","remote"], cwd=repo)
        logline(f"git remote rc={rc}\n{out}\n{err}")
        has_remote = (rc == 0 and out.strip() != "")

        if has_remote:
            rc, o, e = _run(["git","fetch","--all"], cwd=repo)
            logline(f"git fetch --all rc={rc}\n{o}\n{e}")
            if rc != 0:
                return {"ok": False, "job_id": job_id, "error": "git failed: fetch", "job_dir": str(job_dir), "branch": branch}

        rc1, o, e = _run(["git","diff","--quiet"], cwd=repo)
        rc2, o2, e2 = _run(["git","diff","--cached","--quiet"], cwd=repo)
        if rc1 != 0 or rc2 != 0:
            _run(["git","stash","push","-m",f"aios preflight {job_id}"], cwd=repo)
            logline("git stash: repo had tracked changes, stashed")
        rc, o, e = _run(["git","checkout", base], cwd=repo)
        logline(f"git checkout {base} rc={rc}\n{o}\n{e}")
        if rc != 0:
            return {"ok": False, "job_id": job_id, "error": f"git failed: checkout {base}", "job_dir": str(job_dir), "branch": branch}

        if has_remote:
            rc, o, e = _run(["git","pull","--ff-only"], cwd=repo)
            logline(f"git pull --ff-only rc={rc}\n{o}\n{e}")
            if rc != 0:
                return {"ok": False, "job_id": job_id, "error": "git failed: pull --ff-only", "job_dir": str(job_dir), "branch": branch}

        rc, o, e = _run(["git","checkout","-b", branch], cwd=repo)
        logline(f"git checkout -b {branch} rc={rc}\n{o}\n{e}")
        if rc != 0:
            return {"ok": False, "job_id": job_id, "error": f"git failed: checkout -b {branch}", "job_dir": str(job_dir), "branch": branch}

        rc, files, _ = _bash("git ls-files", cwd=repo)
        write("repo_files.txt", files if rc==0 else "")
        rc, stat, _ = _bash("git status -sb", cwd=repo)
        write("repo_status.txt", stat if rc==0 else "")

        
        def _extract_paths(text: str):
            cand = re.findall(r'([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)', text)
            out=[]
            for c in cand:
                c=c.strip().lstrip("./")
                if ".." in c or c.startswith("/"):
                    continue
                out.append(c)
            seen=set()
            uniq=[]
            for x in out:
                if x not in seen:
                    seen.add(x); uniq.append(x)
            return uniq

        sys_prompt = (
            "You are a senior software engineer. "
            "Do NOT use any tools. "
            "Return ONLY a VALID unified diff that git apply can apply. "
            "Do NOT wrap in markdown fences. "
            "The diff MUST include a diff --git header and @@ hunks. "
            "No explanations."
        )
        file_contents = ""
        # Keep context small to avoid TPM blowups.
        targets = _extract_paths(req)
        if targets:
            pick = targets[:5]
        else:
            pick = []
            for fname in files.splitlines():
                fname = fname.strip()
                if fname.startswith(("venv/","runtime/")):
                    continue
                if fname.lower().endswith((".md",".txt",".yml",".yaml",".json")):
                    pick.append(fname)
                if len(pick) >= 25:
                    break

        max_total = 60_000  # hard cap bytes
        total = 0
        for fname in pick:
            fp = repo / fname
            if not fp.exists() or fp.is_dir():
                continue
            try:
                fc = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            chunk = f"\n### {fname}\n```\n{fc}\n```\n"
            if total + len(chunk) > max_total:
                break
            file_contents += chunk
            total += len(chunk)
        user_prompt = f"""Repo: {repo}
Branch: {branch}
Task:
{req}
Files (git ls-files):
{files}
{file_contents}
Rules:
- Output ONLY a unified diff inside one ```diff block.
- The diff must patch existing files, not replace them.
- No prose.
"""

        agent_payload = {
            "mode": payload.get("mode", AIOS_AGENT_MODE),
            "chatInput": user_prompt,
            "systemPrompt": sys_prompt
        }

        resp = _http_post_json(AIOS_AGENT_URL, agent_payload, timeout=AIOS_AGENT_TIMEOUT)
        write("agent_response.json", json.dumps(resp, indent=2, ensure_ascii=False) + "\n")

        answer = resp.get("answer") if isinstance(resp, dict) else None
        if not answer:
            return {"ok": False, "job_id": job_id, "error": "agent returned no answer", "job_dir": str(job_dir), "branch": branch}

        diff = _extract_diff(answer)
        write("patch.diff", diff)
        if not diff.strip():
            return {"ok": False, "job_id": job_id, "error": "no diff found in agent answer", "job_dir": str(job_dir), "branch": branch}

        rc, o, e = _bash("git apply --whitespace=nowarn -", cwd=repo, input_text=diff)
        logline("git apply rc=" + str(rc) + " o=" + o + " e=" + e)
        if rc != 0:
            rc2, o2, e2 = _bash("git apply --reject --whitespace=nowarn -", cwd=repo, input_text=diff)
            logline("git apply --reject rc=" + str(rc2) + " e=" + e2)
            write("git_apply.stderr", e + " " + e2)
            return {"ok": False, "job_id": job_id, "error": "patch apply failed", "job_dir": str(job_dir), "branch": branch}

        rc, o, e = _bash(test_cmd, cwd=repo)
        write("tests.txt", f"cmd: {test_cmd}\nrc: {rc}\n\nSTDOUT:\n{o}\n\nSTDERR:\n{e}\n")
        logline(f"tests rc={rc} cmd={test_cmd}")

        rc1, o1, e1 = _bash("git add -A", cwd=repo)
        logline(f"git add rc={rc1}\n{o1}\n{e1}")

        msg = f"aios job {job_id}: apply agent patch (tests rc={rc})"
        rc2, o2, e2 = _bash(f'git commit -m "{msg}"', cwd=repo)
        logline(f"git commit rc={rc2}\n{o2}\n{e2}")
        if rc2 != 0:
            return {"ok": False, "job_id": job_id, "error": "commit failed", "job_dir": str(job_dir), "branch": branch, "tests_rc": rc}

        write("result.json", json.dumps({
            "ok": True,
            "job_id": job_id,
            "branch": branch,
            "repo_path": str(repo),
            "tests_rc": rc,
            "test_cmd": test_cmd,
        }, indent=2, ensure_ascii=False) + "\n")

        return {"ok": True, "job_id": job_id, "branch": branch, "job_dir": str(job_dir), "tests_rc": rc}

    except Exception as ex:
        write("EXCEPTION.txt", str(ex) + "\n")
        return {"ok": False, "job_id": job_id, "error": f"exception: {ex}", "job_dir": str(job_dir)}
