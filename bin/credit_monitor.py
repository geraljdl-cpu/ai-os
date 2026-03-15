#!/usr/bin/env python3
"""
AI-OS Credit Monitor (Opcional)
Monitoriza uso/custo Anthropic. Degrada graciosamente sem admin key.

Nota: API de usage Anthropic requer Admin API Key com permissão billing.
      Sem admin key, usa estimativa local baseada em tokens registados.

CLI:
  python3 credit_monitor.py              → verifica estado / alerta
  python3 credit_monitor.py record <in> <out> [model]
                                         → regista uso de tokens
"""
import os, json, pathlib, datetime, urllib.request, urllib.error

AIOS_ROOT  = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
USAGE_FILE = AIOS_ROOT / "runtime" / "credit_usage.json"

# Preços aproximados por MTok (USD) — fonte: anthropic.com/pricing
_PRICING = {
    "claude-sonnet-4-6":          {"input": 3.0,  "output": 15.0},
    "claude-opus-4-6":            {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001":  {"input": 0.25, "output": 1.25},
    "claude-haiku-4-5":           {"input": 0.25, "output": 1.25},
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


def _env(k, default=""):
    return os.environ.get(k, default)

def _now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_usage() -> dict:
    try:
        if USAGE_FILE.exists():
            return json.loads(USAGE_FILE.read_text())
    except Exception:
        pass
    return {"tokens_used": 0, "estimated_cost_usd": 0.0, "last_record": None, "alerts_sent": [], "history": []}


def _save_usage(u: dict):
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(u, indent=2, ensure_ascii=False))


def _get_admin_key() -> str:
    key = _env("ANTHROPIC_ADMIN_KEY", "")
    if not key:
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("secrets", AIOS_ROOT / "bin" / "secrets.py")
            sec  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(sec)
            key = sec.get_secret("ANTHROPIC_ADMIN_KEY") or ""
        except Exception:
            pass
    return key


# ── Registo de uso ─────────────────────────────────────────────────────────────

def record_usage(input_tokens: int, output_tokens: int,
                 model: str = "claude-sonnet-4-6") -> dict:
    """Regista uso local de tokens para estimativa de custo."""
    price = _PRICING.get(model, _DEFAULT_PRICING)
    cost  = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000

    u = _load_usage()
    u["tokens_used"]        = u.get("tokens_used", 0) + input_tokens + output_tokens
    u["estimated_cost_usd"] = round(u.get("estimated_cost_usd", 0.0) + cost, 6)
    u["last_record"]        = _now_iso()

    if "history" not in u:
        u["history"] = []
    u["history"].append({
        "ts": _now_iso(), "model": model,
        "in": input_tokens, "out": output_tokens, "cost": round(cost, 6),
    })
    u["history"] = u["history"][-100:]  # reter só últimas 100 entradas

    _save_usage(u)
    return u


# ── Verificação e alertas ──────────────────────────────────────────────────────

def check_credits() -> dict:
    """
    Verifica uso/créditos.
    Com admin key: tenta API Anthropic.
    Sem admin key: usa estimativa local.
    Nunca quebra — degrada graciosamente.
    """
    if _env("ENABLE_CREDIT_MONITOR", "true").lower() != "true":
        return {"ok": True, "skipped": True, "reason": "ENABLE_CREDIT_MONITOR=false"}

    threshold = float(_env("MIN_CREDITS_THRESHOLD", "2.0"))
    u         = _load_usage()
    estimated = u.get("estimated_cost_usd", 0.0)

    # Tentativa de consulta via admin key
    api_data = None
    admin_key = _get_admin_key()
    if admin_key:
        api_data = _fetch_anthropic_usage(admin_key)

    result = {
        "ok":              True,
        "estimated_cost":  estimated,
        "tokens_used":     u.get("tokens_used", 0),
        "threshold_usd":   threshold,
        "last_record":     u.get("last_record"),
        "api_data":        api_data,
        "alert_triggered": False,
        "admin_key_found": bool(admin_key),
    }

    if estimated >= threshold:
        result["alert_triggered"] = True
        result["alert_msg"] = (
            f"Custo estimado Anthropic: ${estimated:.4f} USD >= "
            f"${threshold:.2f} threshold"
        )
        _send_credit_alert(result["alert_msg"], u)

    return result


def _fetch_anthropic_usage(admin_key: str):
    """Tenta obter usage via Anthropic Admin API (requer permissão billing)."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/usage",
            headers={
                "x-api-key":         admin_key,
                "anthropic-version": "2023-06-01",
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {
            "error": f"HTTP_{e.code}",
            "note":  "Admin API key pode não ter permissão de billing",
        }
    except Exception as e:
        return {"error": str(e)}


def _send_credit_alert(msg: str, u: dict):
    """Envia alerta Telegram se não enviado na última hora."""
    alerts = u.get("alerts_sent", [])
    now    = datetime.datetime.utcnow()

    if alerts:
        try:
            last = datetime.datetime.strptime(alerts[-1], "%Y-%m-%dT%H:%M:%SZ")
            if (now - last).total_seconds() < 3600:
                return
        except Exception:
            pass

    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("alerting", AIOS_ROOT / "bin" / "alerting.py")
        ale  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(ale)
        ale.alert_custom(f"💳 *AI-OS Credit Monitor*\n{msg}")
    except Exception:
        pass

    u["alerts_sent"] = (alerts + [_now_iso()])[-10:]
    _save_usage(u)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "record":
        inp = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        out = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        mod = sys.argv[4] if len(sys.argv) > 4 else "claude-sonnet-4-6"
        print(json.dumps(record_usage(inp, out, mod), indent=2))
    else:
        print(json.dumps(check_credits(), indent=2))
