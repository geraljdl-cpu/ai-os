#!/usr/bin/env python3
"""
ai-os Tools Engine v2
Executa tools tipadas com validação, sandbox e audit log.
Tools: bash_safe, write_file, read_file, git_commit, git_status, llm
Routing híbrido: Claude (Anthropic) / Ollama com fallback automático.
"""
import sys, os, json, subprocess, pathlib, datetime, hashlib, urllib.request, urllib.error

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

# Tenta carregar approval_pg; se falhar usa fallback JSON
_apr_mod = None
def _get_apr():
    global _apr_mod
    if _apr_mod is None:
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("approval_pg", AIOS_ROOT / "bin" / "approval_pg.py")
            _apr_mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_apr_mod)
        except Exception:
            _apr_mod = False
    return _apr_mod if _apr_mod else None

def request_approval(tool, inp, job_id=''):
    apr = _get_apr()
    if apr:
        try:
            entry = apr.request_approval(tool=tool, inp=inp, job_id=job_id)
            log(f'APPROVAL_REQUIRED tool={tool} id={entry["id"]} (pg)')
            return entry
        except Exception as e:
            log(f'approval_pg error: {e}, fallback JSON')
    # fallback JSON
    import json as _j
    approvals = []
    if APPROVAL_FILE.exists():
        try: approvals = _j.loads(APPROVAL_FILE.read_text())
        except: pass
    entry = {'id': f'apr_{int(__import__("time").time())}', 'tool': tool, 'input': inp, 'job_id': job_id, 'status': 'pending'}
    approvals.append(entry)
    APPROVAL_FILE.write_text(_j.dumps(approvals, indent=2, ensure_ascii=False))
    log(f'APPROVAL_REQUIRED tool={tool} id={entry["id"]} (json)')
    return entry

def check_approved(tool, inp):
    apr = _get_apr()
    if apr:
        try:
            return apr.check_approved(tool=tool, inp=inp)
        except Exception:
            pass
    # fallback JSON
    import json as _j
    if not APPROVAL_FILE.exists(): return False
    try: approvals = _j.loads(APPROVAL_FILE.read_text())
    except: return False
    for a in approvals:
        if a.get('tool') == tool and a.get('status') == 'approved' and a.get('input') == inp:
            return True
    return False

# ── LLM routing ───────────────────────────────────────────────────────────────

_model_router   = None
_provider_health = None

def _get_model_router():
    global _model_router
    if _model_router is None:
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("model_router", AIOS_ROOT / "bin" / "model_router.py")
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _model_router = mod
        except Exception as e:
            log(f"model_router não carregado: {e}")
            _model_router = False
    return _model_router if _model_router else None


def _get_provider_health():
    global _provider_health
    if _provider_health is None:
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("provider_health", AIOS_ROOT / "bin" / "provider_health.py")
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _provider_health = mod
        except Exception as e:
            log(f"provider_health não carregado: {e}")
            _provider_health = False
    return _provider_health if _provider_health else None


def _call_claude(prompt: str, model: str, system: str = None) -> dict:
    """Chama Anthropic API directamente via urllib."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY_MISSING"}

    messages = [{"role": "user", "content": prompt}]
    body = json.dumps({
        "model":      model,
        "max_tokens": 4096,
        "messages":   messages,
        **({"system": system} if system else {}),
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        text = resp.get("content", [{}])[0].get("text", "")
        usage = resp.get("usage", {})
        # regista tokens no credit monitor (best-effort)
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("credit_monitor", AIOS_ROOT / "bin" / "credit_monitor.py")
            cm   = _ilu.module_from_spec(spec)
            spec.loader.exec_module(cm)
            cm.record_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0), model)
        except Exception:
            pass
        return {"ok": True, "text": text, "usage": usage, "provider": "claude", "model": model}

    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:300]
        code = e.code
        if   code == 401: err = "AUTH_ERROR"
        elif code == 429: err = "RATE_LIMIT"
        elif code >= 500: err = "SERVER_ERROR"
        else:             err = f"HTTP_{code}"
        return {"ok": False, "error": err, "detail": err_body, "recoverable": code in (429, 500, 503)}

    except Exception as e:
        return {"ok": False, "error": f"NETWORK_{type(e).__name__}", "detail": str(e), "recoverable": True}


def _call_ollama(prompt: str, model: str, system: str = None) -> dict:
    """Chama Ollama local via urllib."""
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    msg = [{"role": "user", "content": prompt}]
    if system:
        msg = [{"role": "system", "content": system}] + msg

    body = json.dumps({
        "model":    model,
        "messages": msg,
        "stream":   False,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        text = resp.get("message", {}).get("content", "")
        return {"ok": True, "text": text, "provider": "ollama", "model": model}
    except Exception as e:
        return {"ok": False, "error": f"OLLAMA_{type(e).__name__}", "detail": str(e), "recoverable": False}


def call_llm(prompt: str, job: dict = None, system: str = None) -> dict:
    """
    Chama o provider LLM certo com fallback automático.
    Regista provider escolhido, motivo e se houve fallback.
    """
    if job is None: job = {}

    mr = _get_model_router()
    ph = _get_provider_health()

    # Obter estado de saúde dos providers
    system_state = {}
    if ph:
        try:
            ps = ph.get_provider_state()
            system_state = {
                "claude_available": ps["claude"]["available"],
                "ollama_available": ps["ollama"]["available"],
            }
        except Exception:
            pass

    # Decidir provider
    if mr:
        decision = mr.decide_model(job=job, system_state=system_state)
    else:
        # fallback se model_router não carregar
        decision = {
            "provider":         "claude",
            "model":            os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            "reason":           "model_router_unavailable",
            "fallback_allowed": True,
        }

    provider = decision.get("provider")
    model    = decision.get("model")
    reason   = decision.get("reason", "")
    job_id   = job.get("id", job.get("job_id", ""))

    if not provider:
        log(f"LLM job={job_id} error=all_providers_unavailable")
        return {"ok": False, "error": decision.get("error", "no_provider"), "job_id": job_id}

    log(f"LLM job={job_id} provider={provider} model={model} reason={reason}")

    # Chamar provider escolhido
    if provider == "claude":
        result = _call_claude(prompt, model, system)
    else:
        result = _call_ollama(prompt, model, system)

    # Fallback automático se falhou e é recuperável
    if not result["ok"] and decision.get("fallback_allowed"):
        orig_err   = result.get("error", "")
        fallback_p = "ollama" if provider == "claude" else "claude"
        fallback_m = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b") if fallback_p == "ollama" \
                     else os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

        log(f"LLM job={job_id} fallback provider={fallback_p} original_error={orig_err}")

        if fallback_p == "claude":
            result = _call_claude(prompt, fallback_m, system)
        else:
            result = _call_ollama(prompt, fallback_m, system)

        result["fallback"]         = True
        result["original_provider"] = provider
        result["original_error"]    = orig_err

        if ph:
            try:
                ph.record_routing(fallback_p, f"fallback:{orig_err}", job_id=job_id,
                                  fallback=True, original_error=orig_err)
            except Exception:
                pass
    else:
        if ph:
            try:
                ph.record_routing(provider, reason, job_id=job_id, fallback=False)
            except Exception:
                pass

    result["job_id"]   = job_id
    result["decision"] = decision
    return result


def tool_llm(p: dict) -> dict:
    """Tool 'llm': chama o provider LLM com routing híbrido."""
    prompt = p.get("prompt") or p.get("text") or p.get("message") or ""
    system = p.get("system")
    job    = p.get("job") or {}
    if not prompt:
        return {"ok": False, "error": "prompt obrigatório"}
    return call_llm(prompt, job=job, system=system)


TOOLS = {
    "bash_safe":   lambda p: tool_bash_safe(p.get("cmd",""), p.get("cwd")),
    "bash":        lambda p: tool_bash_safe(p.get("cmd",""), p.get("cwd")),
    "write_file":  lambda p: tool_write_file(p.get("path",""), p.get("content","")),
    "read_file":   lambda p: tool_read_file(p.get("path","")),
    "git_commit":  lambda p: tool_git_commit(p.get("msg","auto-commit"), p.get("cwd")),
    "git_status":  lambda p: tool_git_status(p.get("cwd")),
    "llm":         tool_llm,
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

# Factory / Modbus tools integration
try:
    _fspec = importlib.util.spec_from_file_location("tools_factory", os.path.join(AIOS_ROOT, "bin/tools_factory.py"))
    _fac = importlib.util.module_from_spec(_fspec)
    _fspec.loader.exec_module(_fac)
    TOOLS.update({k: (lambda f: lambda p: f(p))(v) for k,v in _fac.TOOLS.items()})
    log("factory tools loaded: " + str(list(_fac.TOOLS.keys())))
except Exception as e:
    log(f"factory tools not loaded: {e}")

# DMX / Art-Net tools integration
try:
    _dspec = importlib.util.spec_from_file_location("tools_dmx", os.path.join(AIOS_ROOT, "bin/tools_dmx.py"))
    _dmx = importlib.util.module_from_spec(_dspec)
    _dspec.loader.exec_module(_dmx)
    TOOLS.update({k: (lambda f: lambda p: f(p))(v) for k,v in _dmx.TOOLS.items()})
    log("dmx tools loaded: " + str(list(_dmx.TOOLS.keys())))
except Exception as e:
    log(f"dmx tools not loaded: {e}")

if __name__ == "__main__":
    data = json.load(sys.stdin)
    steps = data if isinstance(data, list) else data.get("steps", [])
    log_path = data.get("log_path") if isinstance(data, dict) else None
    results = run(steps, log_path)
    print(json.dumps({"ok": True, "results": results}, indent=2))
