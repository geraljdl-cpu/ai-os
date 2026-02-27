#!/usr/bin/env python3
"""
ai-os Tools Engine v1
Executa tools tipadas com validação, sandbox e audit log.
Tools: bash_safe, write_file, read_file, git_commit, git_status
"""
import sys, os, json, subprocess, pathlib, datetime, hashlib

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os"))).resolve()
WORKSPACE = AIOS_ROOT / "workspace"
AIOS_MODE = os.environ.get("AIOS_MODE", "simulate")

DENIED = ["rm -rf","dd ","mkfs","shutdown","reboot","sudo","chmod 777","curl|sh","wget|sh",":(){","> /dev/sd","> /dev/hd"]
ALLOWED_CMDS = ["echo","cat","ls","mkdir","cp","mv","python","python3","git","docker","pip","pip3","touch","head","tail","grep","wc","find"]

LOG_PATH = None

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if LOG_PATH:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")

def is_safe_path(p):
    try:
        return pathlib.Path(p).resolve().is_relative_to(AIOS_ROOT)
    except:
        return False

def is_allowed_cmd(cmd):
    for d in DENIED:
        if d in cmd:
            return False, f"denied pattern: {d}"
    first = cmd.strip().split()[0] if cmd.strip() else ""
    if not any(first == a or first.endswith("/"+a) for a in ALLOWED_CMDS):
        return False, f"cmd not in allowlist: {first}"
    return True, "ok"

def tool_bash_safe(cmd, cwd=None):
    ok, reason = is_allowed_cmd(cmd)
    if not ok:
        return {"ok": False, "blocked": reason, "stdout": "", "stderr": "", "code": 99}
    if AIOS_MODE == "simulate":
        return {"ok": True, "simulated": True, "cmd": cmd, "stdout": f"[SIMULATE] {cmd}", "code": 0}
    work_dir = str(AIOS_ROOT / (cwd or ""))
    try:
        r = subprocess.run(cmd, shell=True, cwd=work_dir, capture_output=True, text=True, timeout=60)
        return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "TIMEOUT", "code": 124}

def tool_write_file(path, content):
    if not is_safe_path(path):
        return {"ok": False, "error": f"path outside sandbox: {path}"}
    if AIOS_MODE == "simulate":
        return {"ok": True, "simulated": True, "path": path, "bytes": len(content)}
    p = pathlib.Path(path)
    if not p.is_absolute():
        p = AIOS_ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    return {"ok": True, "path": str(p), "bytes": len(content), "hash": h}

def tool_read_file(path):
    p = pathlib.Path(path)
    if not p.is_absolute():
        p = AIOS_ROOT / path
    if not is_safe_path(str(p)):
        return {"ok": False, "error": "path outside sandbox"}
    if not p.exists():
        return {"ok": False, "error": "file not found"}
    return {"ok": True, "content": p.read_text(encoding="utf-8", errors="replace")}

def tool_git_commit(msg, cwd=None):
    work_dir = str(AIOS_ROOT / (cwd or ""))
    if AIOS_MODE == "simulate":
        return {"ok": True, "simulated": True, "msg": msg}
    r1 = subprocess.run("git add -A", shell=True, cwd=work_dir, capture_output=True, text=True)
    r2 = subprocess.run(f'git commit -m "{msg}"', shell=True, cwd=work_dir, capture_output=True, text=True)
    return {"ok": r2.returncode == 0, "stdout": r2.stdout.strip(), "stderr": r2.stderr.strip()}

def tool_git_status(cwd=None):
    work_dir = str(AIOS_ROOT / (cwd or ""))
    r = subprocess.run("git status --short", shell=True, cwd=work_dir, capture_output=True, text=True)
    return {"ok": True, "output": r.stdout.strip()}

# Tools que requerem aprovação humana antes de executar
APPROVAL_REQUIRED = {
    'toc_invoice_create',
    'toc_customer_create',
    'git_commit',
    'write_file',
}

APPROVAL_FILE = AIOS_ROOT / 'runtime' / 'pending_approvals.json'

def request_approval(tool, inp, job_id=''):
    import json as _j
    approvals = []
    if APPROVAL_FILE.exists():
        try: approvals = _j.loads(APPROVAL_FILE.read_text())
        except: approvals = []
    entry = {'id': f'apr_{int(__import__("time").time())}', 'tool': tool, 'input': inp, 'job_id': job_id, 'status': 'pending'}
    approvals.append(entry)
    APPROVAL_FILE.write_text(_j.dumps(approvals, indent=2, ensure_ascii=False))
    log(f'APPROVAL_REQUIRED tool={tool} id={entry["id"]}')
    return entry

def check_approved(tool, inp):
    import json as _j
    if not APPROVAL_FILE.exists(): return False
    try: approvals = _j.loads(APPROVAL_FILE.read_text())
    except: return False
    for a in approvals:
        if a.get('tool') == tool and a.get('status') == 'approved' and a.get('input') == inp:
            return True
    return False

TOOLS = {
    "bash_safe":   lambda p: tool_bash_safe(p.get("cmd",""), p.get("cwd")),
    "bash":        lambda p: tool_bash_safe(p.get("cmd",""), p.get("cwd")),
    "write_file":  lambda p: tool_write_file(p.get("path",""), p.get("content","")),
    "read_file":   lambda p: tool_read_file(p.get("path","")),
    "git_commit":  lambda p: tool_git_commit(p.get("msg","auto-commit"), p.get("cwd")),
    "git_status":  lambda p: tool_git_status(p.get("cwd")),
}

def run(steps, log_path=None):
    global LOG_PATH
    LOG_PATH = log_path
    results = []
    for i, step in enumerate(steps):
        tool = step.get("tool","bash")
        inp  = step.get("input", step)
        if isinstance(inp, str):
            inp = {"cmd": inp}
        log(f"step[{i}] tool={tool} input={json.dumps(inp)[:120]}")
        fn = TOOLS.get(tool)
        if not fn:
            result = {"ok": False, "error": f"unknown tool: {tool}"}
        elif tool in APPROVAL_REQUIRED and AIOS_MODE == "live" and not check_approved(tool, inp):
            entry = request_approval(tool, inp)
            result = {"ok": False, "pending_approval": True, "approval_id": entry["id"], "msg": f"Tool {tool} requer aprovacao humana."}
        else:
            try:
                result = fn(inp)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        log(f"step[{i}] result={json.dumps(result)[:200]}")
        results.append({"tool": tool, "input": inp, "result": result})
    return results

# Finance tools integration
try:
    import importlib.util, sys as _sys
    _spec = importlib.util.spec_from_file_location("tools_finance", os.path.join(AIOS_ROOT, "bin/tools_finance.py"))
    _fin = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_fin)
    TOOLS.update({k: (lambda f: lambda p: f(p))(v) for k,v in _fin.TOOLS.items()})
    log("finance tools loaded: " + str(list(_fin.TOOLS.keys())))
except Exception as e:
    log(f"finance tools not loaded: {e}")

if __name__ == "__main__":
    data = json.load(sys.stdin)
    steps = data if isinstance(data, list) else data.get("steps", [])
    log_path = data.get("log_path") if isinstance(data, dict) else None
    results = run(steps, log_path)
    print(json.dumps({"ok": True, "results": results}, indent=2))
