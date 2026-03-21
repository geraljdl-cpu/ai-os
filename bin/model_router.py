#!/usr/bin/env python3
"""
AI-OS Model Router — Hybrid Tier v2
Decide qual provider LLM usar: Claude | Ollama asus_gpu | Ollama cluster_cpu.

decide_model(job, env, system_state) -> dict
  provider: "claude" | "asus_gpu" | "cluster_cpu" | None
  ollama_url: endpoint Ollama (se provider != "claude")
  model:    nome do modelo
  reason:   string curta
  fallback_allowed: bool
"""
import os, json, pathlib, datetime, urllib.request, urllib.error

AIOS_ROOT     = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
OVERRIDE_FILE = AIOS_ROOT / "runtime" / "model_override.json"

# ── Provider Registry ───────────────────────────────────────────────────────────
# Cada entry: url, model, tier (1=asus_gpu, 2=cluster_cpu), tag
PROVIDERS = [
    {"name": "asus_gpu",   "url": "http://localhost:11434",     "model": "qwen2.5-coder:7b", "tier": 1},
    {"name": "node1_cpu",  "url": "http://localhost:11435",     "model": "qwen2.5-coder:7b", "tier": 2},
    {"name": "node2_cpu",  "url": "http://192.168.1.112:11434", "model": "qwen2.5-coder:7b", "tier": 2},
    {"name": "node4_cpu",  "url": "http://192.168.1.122:11434", "model": "qwen2.5-coder:7b", "tier": 2},
    {"name": "node3_cpu",  "url": "http://192.168.1.121:11434", "model": "qwen2.5-coder:7b", "tier": 2},
]

# ── Config ─────────────────────────────────────────────────────────────────────

def _env(k, default=""):
    return os.environ.get(k, default)

def get_config():
    return {
        "hybrid_mode":  _env("HYBRID_MODE",  "true").lower() == "true",
        "force_model":  _env("FORCE_MODEL",  "").lower().strip(),
        "ollama_url":   _env("OLLAMA_URL",   "http://localhost:11434"),
        "ollama_model": _env("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "claude_model": _env("CLAUDE_MODEL", "claude-sonnet-4-6"),
    }

# ── Healthcheck ─────────────────────────────────────────────────────────────────

def check_provider_health(url: str, timeout: int = 2) -> bool:
    """Verifica se endpoint Ollama está up via /api/tags."""
    try:
        req = urllib.request.Request(f"{url}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def get_healthy_providers(tier: int = None) -> list:
    """Retorna lista de providers saudáveis, opcionalmente filtrado por tier."""
    result = []
    for p in PROVIDERS:
        if tier is not None and p["tier"] != tier:
            continue
        if check_provider_health(p["url"]):
            result.append(p)
    return result

# ── Heurística ─────────────────────────────────────────────────────────────────

# Tipos de task → Claude (complexo / alto impacto)
_CLAUDE_TYPES = {"DEV_TASK", "ARCH_TASK", "REFACTOR", "CODE_GEN", "MULTI_FILE"}

# Tipos de task → asus_gpu (coding assist local, rápido)
_ASUS_TYPES = {"CODING", "CODE_REVIEW", "DEBUG", "EXPLAIN", "PATCH"}

# Keywords → Claude
_CLAUDE_KEYWORDS = [
    "refactor", "arquitetura", "architecture", "multi-file", "design",
    "implement", "implementar", "create module", "criar módulo",
    "complex", "complexo", "critical", "crítico", "security", "segurança",
    "generate code", "gerar código", "analyse", "analisar", "estrutura",
    "integra", "migra", "migration", "database schema", "api design",
]

# Keywords → asus_gpu (coding local)
_ASUS_KEYWORDS = [
    "code", "código", "function", "função", "fix bug", "debug",
    "review", "patch", "diff", "rename", "refactor small",
    "write test", "escrever teste", "docstring",
]

# Keywords → cluster_cpu (simples / rotina / batch)
_CLUSTER_KEYWORDS = [
    "monitoring", "monitorização", "classify", "classificar",
    "triage", "triagem", "status", "estado", "rotina", "routine",
    "alert", "alerta", "log", "check", "verificar", "summary", "resumo",
    "health", "saúde", "report", "relatório", "listar", "list",
    "ping", "temperatura", "pressão", "rpm", "inbox", "classificar",
]

_CLUSTER_TYPES = {"OPS", "MONITORING", "CLASSIFY", "TRIAGE", "ROUTINE", "REPORT"}


def _heuristic(job: dict) -> tuple[str, str]:
    """Retorna (provider_name, reason): 'claude' | 'asus_gpu' | 'cluster_cpu'."""
    task_type = (job.get("task_type") or job.get("type") or "").upper().strip()
    goal      = (job.get("goal") or job.get("title") or job.get("description") or "").lower()
    priority  = int(job.get("priority") or job.get("p") or 5)

    if task_type in _CLAUDE_TYPES:
        return "claude", f"task_type={task_type}"
    if task_type in _ASUS_TYPES:
        return "asus_gpu", f"task_type={task_type}"
    if task_type in _CLUSTER_TYPES:
        return "cluster_cpu", f"task_type={task_type}"

    if priority >= 8:
        return "claude", f"high_priority={priority}"

    for kw in _CLAUDE_KEYWORDS:
        if kw in goal:
            return "claude", f"keyword={kw}"

    for kw in _ASUS_KEYWORDS:
        if kw in goal:
            return "asus_gpu", f"keyword={kw}"

    for kw in _CLUSTER_KEYWORDS:
        if kw in goal:
            return "cluster_cpu", f"keyword={kw}"

    return "cluster_cpu", "default_simple"


# ── Decide ─────────────────────────────────────────────────────────────────────

def decide_model(job: dict = None, env: dict = None, system_state: dict = None) -> dict:
    """
    Retorna dict com:
      provider:         "claude" | "asus_gpu" | "cluster_cpu" | None
      ollama_url:       endpoint (se Ollama)
      model:            nome do modelo
      reason:           string curta
      fallback_allowed: bool
    """
    if job is None:          job = {}
    if env is None:          env = {}
    if system_state is None: system_state = {}

    cfg = get_config()

    # 1. FORCE_MODEL override
    force = cfg["force_model"] or _get_runtime_override()
    if force in ("claude", "asus_gpu", "cluster_cpu", "ollama"):
        if force == "ollama":
            force = "asus_gpu"  # backward compat
        if force == "claude":
            return {"provider": "claude", "model": cfg["claude_model"],
                    "reason": f"FORCE_MODEL={force}", "fallback_allowed": False}
        # For Ollama tiers, pick first healthy provider of that tier
        tier = 1 if force == "asus_gpu" else 2
        providers = get_healthy_providers(tier)
        if providers:
            p = providers[0]
            return {"provider": force, "ollama_url": p["url"], "model": p["model"],
                    "reason": f"FORCE_MODEL={force}", "fallback_allowed": False}
        return {"provider": None, "model": None, "reason": f"FORCE_MODEL={force}_unavailable",
                "fallback_allowed": False, "error": f"Nenhum provider {force} disponível"}

    # 2. HYBRID_MODE=false → só Claude
    if not cfg["hybrid_mode"]:
        return {"provider": "claude", "model": cfg["claude_model"],
                "reason": "HYBRID_MODE=false", "fallback_allowed": False}

    # 3. Heurística → preferred provider
    preferred, reason = _heuristic(job)

    # 4. Healthcheck e fallback
    def _pick_ollama(tier: int):
        providers = get_healthy_providers(tier)
        if providers:
            p = providers[0]
            return {"provider": "asus_gpu" if tier == 1 else "cluster_cpu",
                    "ollama_url": p["url"], "model": p["model"]}
        return None

    if preferred == "claude":
        claude_ok = system_state.get("claude_available", True)
        if not claude_ok:
            # fallback → asus_gpu → cluster_cpu
            r = _pick_ollama(1) or _pick_ollama(2)
            if r:
                return {**r, "reason": f"fallback:claude_unavailable ({reason})",
                        "fallback_allowed": True, "original_provider": "claude"}
            return {"provider": None, "model": None, "reason": "all_unavailable",
                    "fallback_allowed": False, "error": "Nenhum provider disponível"}
        return {"provider": "claude", "model": cfg["claude_model"],
                "reason": reason, "fallback_allowed": True}

    # asus_gpu or cluster_cpu
    target_tier = 1 if preferred == "asus_gpu" else 2
    r = _pick_ollama(target_tier)
    if r:
        return {**r, "reason": reason, "fallback_allowed": True}

    # tier unavailable → try other tier
    alt_tier = 2 if target_tier == 1 else 1
    r = _pick_ollama(alt_tier)
    if r:
        return {**r, "reason": f"fallback:tier{target_tier}_unavailable ({reason})",
                "fallback_allowed": True, "original_provider": preferred}

    # all Ollama down → claude
    claude_ok = system_state.get("claude_available", True)
    if claude_ok:
        return {"provider": "claude", "model": cfg["claude_model"],
                "reason": f"fallback:ollama_unavailable ({reason})",
                "fallback_allowed": True, "original_provider": preferred}

    return {"provider": None, "model": None, "reason": "all_unavailable",
            "fallback_allowed": False, "error": "Nenhum provider disponível"}


# ── Runtime override (UI toggle) ───────────────────────────────────────────────

def _get_runtime_override() -> str:
    try:
        if OVERRIDE_FILE.exists():
            data = json.loads(OVERRIDE_FILE.read_text())
            return (data.get("force_model") or "").lower().strip()
    except Exception:
        pass
    return ""


def set_runtime_override(force_model: str):
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(json.dumps({
        "force_model": force_model.lower().strip() if force_model else "",
        "set_at":      datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2))


def get_runtime_override() -> str:
    return _get_runtime_override()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "set_override":
        val = sys.argv[2] if len(sys.argv) > 2 else ""
        set_runtime_override(val)
        print(json.dumps({"ok": True, "force_model": val or "(cleared)"}))

    elif len(sys.argv) > 1 and sys.argv[1] == "get_override":
        print(json.dumps({"force_model": _get_runtime_override() or "(none)"}))

    elif len(sys.argv) > 1 and sys.argv[1] == "health":
        result = {}
        for p in PROVIDERS:
            result[p["name"]] = {"url": p["url"], "up": check_provider_health(p["url"]),
                                  "tier": p["tier"], "model": p["model"]}
        print(json.dumps(result, indent=2))

    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        healthy_1 = get_healthy_providers(1)
        healthy_2 = get_healthy_providers(2)
        print(json.dumps({
            "asus_gpu":   [p["name"] for p in healthy_1],
            "cluster_cpu": [p["name"] for p in healthy_2],
            "total_up": len(healthy_1) + len(healthy_2),
        }, indent=2))

    else:
        job   = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        state = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(decide_model(job=job, system_state=state), indent=2))
