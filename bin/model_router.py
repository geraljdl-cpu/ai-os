#!/usr/bin/env python3
"""
AI-OS Model Router
Decide qual provider LLM usar: Claude (Anthropic) ou Ollama (local).

decide_model(job, env, system_state) -> dict
  provider: "claude" | "ollama" | None
  model:    nome do modelo
  reason:   string curta
  fallback_allowed: bool
"""
import os, json, pathlib, datetime

AIOS_ROOT     = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
OVERRIDE_FILE = AIOS_ROOT / "runtime" / "model_override.json"

# ── Config ─────────────────────────────────────────────────────────────────────

def _env(k, default=""):
    return os.environ.get(k, default)

def get_config():
    return {
        "hybrid_mode":  _env("HYBRID_MODE",  "true").lower() == "true",
        "force_model":  _env("FORCE_MODEL",  "").lower().strip(),
        "ollama_url":   _env("OLLAMA_URL",   "http://localhost:11434"),
        "ollama_model": _env("OLLAMA_MODEL", "qwen2.5:14b"),
        "claude_model": _env("CLAUDE_MODEL", "claude-sonnet-4-6"),
    }

# ── Heurística ─────────────────────────────────────────────────────────────────

# Tipos de task que mapeiam para Claude (complexo / alto impacto)
_CLAUDE_TYPES = {"DEV_TASK", "ARCH_TASK", "REFACTOR", "CODE_GEN", "MULTI_FILE"}

# Keywords no goal/description que indicam complexidade → Claude
_CLAUDE_KEYWORDS = [
    "refactor", "arquitetura", "architecture", "multi-file", "design",
    "implement", "implementar", "create module", "criar módulo",
    "complex", "complexo", "critical", "crítico", "security", "segurança",
    "generate code", "gerar código", "analyse", "analisar", "estrutura",
    "integra", "migra", "migration", "database schema", "api design",
]

# Tipos de task que mapeiam para Ollama (simples / rotina)
_OLLAMA_TYPES = {"OPS", "MONITORING", "CLASSIFY", "TRIAGE", "ROUTINE", "REPORT"}

# Keywords que indicam tarefa simples → Ollama
_OLLAMA_KEYWORDS = [
    "monitoring", "monitorização", "classify", "classificar",
    "triage", "triagem", "status", "estado", "rotina", "routine",
    "alert", "alerta", "log", "check", "verificar", "summary", "resumo",
    "health", "saúde", "report", "relatório", "listar", "list",
    "ping", "temperatura", "pressão", "rpm",
]


def _heuristic(job: dict) -> tuple[str, str]:
    """Retorna (provider, reason) com base no tipo/conteúdo da task."""
    task_type = (job.get("task_type") or job.get("type") or "").upper().strip()
    goal      = (job.get("goal") or job.get("title") or job.get("description") or "").lower()
    priority  = int(job.get("priority") or job.get("p") or 5)

    # Tipo explícito → Claude
    if task_type in _CLAUDE_TYPES:
        return "claude", f"task_type={task_type}"

    # Tipo explícito → Ollama
    if task_type in _OLLAMA_TYPES:
        return "ollama", f"task_type={task_type}"

    # Prioridade alta → Claude (impacto estrutural)
    if priority >= 8:
        return "claude", f"high_priority={priority}"

    # Análise por keywords do goal
    for kw in _CLAUDE_KEYWORDS:
        if kw in goal:
            return "claude", f"keyword={kw}"

    for kw in _OLLAMA_KEYWORDS:
        if kw in goal:
            return "ollama", f"keyword={kw}"

    # Default: Ollama (mais económico e rápido para tarefas genéricas)
    return "ollama", "default_simple"


# ── Decide ─────────────────────────────────────────────────────────────────────

def decide_model(job: dict = None, env: dict = None, system_state: dict = None) -> dict:
    """
    Retorna dict com:
      provider:         "claude" | "ollama" | None
      model:            nome do modelo
      reason:           string curta
      fallback_allowed: bool
    """
    if job is None:          job = {}
    if env is None:          env = {}
    if system_state is None: system_state = {}

    cfg = get_config()

    # 1. FORCE_MODEL — env var tem prioridade sobre override de runtime
    force = cfg["force_model"] or _get_runtime_override()
    if force in ("claude", "ollama"):
        model = cfg["claude_model"] if force == "claude" else cfg["ollama_model"]
        return {
            "provider":         force,
            "model":            model,
            "reason":           f"FORCE_MODEL={force}",
            "fallback_allowed": False,
        }

    # 2. HYBRID_MODE=false → só Claude, sem fallback
    if not cfg["hybrid_mode"]:
        return {
            "provider":         "claude",
            "model":            cfg["claude_model"],
            "reason":           "HYBRID_MODE=false",
            "fallback_allowed": False,
        }

    # 3. Saúde dos providers (injetada ou assumida como true se não disponível)
    claude_ok = system_state.get("claude_available", True)
    ollama_ok = system_state.get("ollama_available", True)

    # 4. Heurística
    preferred, reason = _heuristic(job)

    # 5. Aplicar disponibilidade
    if preferred == "claude" and not claude_ok:
        if ollama_ok:
            return {
                "provider":          "ollama",
                "model":             cfg["ollama_model"],
                "reason":            f"fallback:claude_unavailable (original: {reason})",
                "fallback_allowed":  True,
                "original_provider": "claude",
                "original_reason":   reason,
            }
        return {
            "provider":         None,
            "model":            None,
            "reason":           "all_providers_unavailable",
            "fallback_allowed": False,
            "error":            "Nenhum provider disponível",
        }

    if preferred == "ollama" and not ollama_ok:
        if claude_ok:
            return {
                "provider":          "claude",
                "model":             cfg["claude_model"],
                "reason":            f"fallback:ollama_unavailable (original: {reason})",
                "fallback_allowed":  True,
                "original_provider": "ollama",
                "original_reason":   reason,
            }
        return {
            "provider":         None,
            "model":            None,
            "reason":           "all_providers_unavailable",
            "fallback_allowed": False,
            "error":            "Nenhum provider disponível",
        }

    model = cfg["claude_model"] if preferred == "claude" else cfg["ollama_model"]
    return {
        "provider":         preferred,
        "model":            model,
        "reason":           reason,
        "fallback_allowed": True,
    }


# ── Runtime override (UI toggle) ───────────────────────────────────────────────

def _get_runtime_override() -> str:
    """Lê FORCE_MODEL do arquivo de override de runtime (definido pela UI)."""
    try:
        if OVERRIDE_FILE.exists():
            data = json.loads(OVERRIDE_FILE.read_text())
            return (data.get("force_model") or "").lower().strip()
    except Exception:
        pass
    return ""


def set_runtime_override(force_model: str):
    """Define ou limpa o override de FORCE_MODEL em runtime."""
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(json.dumps({
        "force_model": force_model.lower().strip() if force_model else "",
        "set_at":      datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2))


def get_runtime_override() -> str:
    """Exposta para uso externo."""
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
    else:
        job   = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        state = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(decide_model(job=job, system_state=state), indent=2))
