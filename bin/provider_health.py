#!/usr/bin/env python3
"""
AI-OS Provider Health
Verifica disponibilidade de Claude (Anthropic) e Ollama (local).
Guarda estado em runtime/provider_state.json (cache TTL=60s) para evitar flapping.

CLI:
  python3 provider_health.py             → estado actual (com cache)
  python3 provider_health.py --force     → força re-verificação
"""
import os, json, pathlib, datetime, urllib.request, urllib.error

AIOS_ROOT  = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
STATE_FILE = AIOS_ROOT / "runtime" / "provider_state.json"

CACHE_TTL_SECS = 60   # não re-verificar mais do que 1x por minuto

# ── Persistência ───────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_fresh(ts_iso: str, ttl: int = CACHE_TTL_SECS) -> bool:
    try:
        ts  = datetime.datetime.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.datetime.utcnow() - ts).total_seconds()
        return age < ttl
    except Exception:
        return False


def _update_provider(provider: str, available: bool, error, state: dict):
    if provider not in state:
        state[provider] = {}
    state[provider]["available"]  = available
    state[provider]["last_check"] = _now_iso()
    state[provider]["last_error"] = error
    _save_state(state)


# ── Claude health ──────────────────────────────────────────────────────────────

def check_anthropic_available(force: bool = False) -> dict:
    """Verifica se Anthropic API está acessível. Usa cache TTL=60s."""
    state = _load_state()
    c     = state.get("claude", {})
    if not force and c.get("last_check") and _is_fresh(c["last_check"]):
        return {"available": c.get("available", False), "cached": True, "error": c.get("last_error")}

    # Obter API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("secrets", AIOS_ROOT / "bin" / "secrets.py")
            sec  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(sec)
            api_key = sec.get_secret("ANTHROPIC_API_KEY") or ""
        except Exception:
            pass

    if not api_key:
        _update_provider("claude", False, "ANTHROPIC_API_KEY_MISSING", state)
        return {"available": False, "error": "ANTHROPIC_API_KEY_MISSING", "cached": False}

    # Testar com endpoint leve (models list)
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            }
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        _update_provider("claude", True, None, state)
        return {"available": True, "cached": False, "error": None}

    except urllib.error.HTTPError as e:
        err = f"HTTP_{e.code}"
        if   e.code == 401:    err = "AUTH_ERROR"
        elif e.code == 403:    err = "FORBIDDEN"
        elif e.code == 429:    err = "RATE_LIMIT"
        elif e.code >= 500:    err = "SERVER_ERROR"
        _update_provider("claude", False, err, state)
        return {"available": False, "error": err, "cached": False}

    except Exception as e:
        err = f"NETWORK_{type(e).__name__}"
        _update_provider("claude", False, err, state)
        return {"available": False, "error": err, "cached": False}


# ── Ollama health ──────────────────────────────────────────────────────────────

def check_ollama_available(force: bool = False) -> dict:
    """Verifica se Ollama local está acessível. Usa cache TTL=60s."""
    state = _load_state()
    o     = state.get("ollama", {})
    if not force and o.get("last_check") and _is_fresh(o["last_check"]):
        return {"available": o.get("available", False), "cached": True, "error": o.get("last_error")}

    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
        _update_provider("ollama", True, None, state)
        return {"available": True, "cached": False, "error": None}
    except Exception as e:
        err = f"OLLAMA_{type(e).__name__}"
        _update_provider("ollama", False, err, state)
        return {"available": False, "error": err, "cached": False}


# ── Estado combinado ───────────────────────────────────────────────────────────

def get_provider_state(force: bool = False) -> dict:
    """Retorna estado combinado de ambos os providers."""
    claude = check_anthropic_available(force=force)
    ollama = check_ollama_available(force=force)
    state  = _load_state()
    return {
        "claude": {
            "available":  claude["available"],
            "error":      claude.get("error"),
            "last_check": state.get("claude", {}).get("last_check"),
        },
        "ollama": {
            "available":  ollama["available"],
            "error":      ollama.get("error"),
            "last_check": state.get("ollama", {}).get("last_check"),
        },
        "last_fallback": state.get("last_fallback"),
        "last_routing":  state.get("last_routing"),
        "ts": _now_iso(),
    }


def record_routing(provider: str, reason: str, job_id: str = "",
                   fallback: bool = False, original_error: str = None):
    """Regista uma decisão de routing no estado persistido."""
    state = _load_state()
    entry = {
        "provider": provider,
        "reason":   reason,
        "job_id":   job_id,
        "ts":       _now_iso(),
    }
    state["last_routing"] = entry
    if fallback:
        state["last_fallback"] = {**entry, "original_error": original_error}
    _save_state(state)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    print(json.dumps(get_provider_state(force=force), indent=2))
