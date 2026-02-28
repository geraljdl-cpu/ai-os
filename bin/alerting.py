#!/usr/bin/env python3
"""
AI-OS Alerting — Telegram bot.
Credenciais: TELEGRAM_TOKEN e TELEGRAM_CHAT_ID via secrets store ou env vars.

Triggers suportados:
  - worker_crash    — autopilot/worker morreu inesperadamente
  - job_failed      — job falhou (incluído >3x se attempts passado)
  - db_down         — DB inacessível
  - token_expired   — JWT expirado em chamada de API
  - backup_failed   — backup postgres falhou
  - custom          — mensagem livre

CLI:
  python3 alerting.py test                          → envia mensagem de teste
  python3 alerting.py send <trigger> [msg]          → envia alerta
  python3 alerting.py worker_crash [job_id]
  python3 alerting.py job_failed <job_id> <attempts>
  python3 alerting.py db_down
  python3 alerting.py token_expired
  python3 alerting.py backup_failed [detail]
"""
import os, sys, json, pathlib, datetime, urllib.request, urllib.error

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))

# ── Credenciais ───────────────────────────────────────────────────────────────

def _get_creds() -> tuple[str, str]:
    """Retorna (token, chat_id) — tenta secrets, depois env vars."""
    token   = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        try:
            import importlib.util as _ilu
            spec  = _ilu.spec_from_file_location("secrets", AIOS_ROOT / "bin" / "secrets.py")
            sec   = _ilu.module_from_spec(spec)
            spec.loader.exec_module(sec)
            token   = token   or sec.get_secret("TELEGRAM_TOKEN")   or ""
            chat_id = chat_id or sec.get_secret("TELEGRAM_CHAT_ID") or ""
        except Exception:
            pass

    return token, chat_id


# ── Send ──────────────────────────────────────────────────────────────────────

def send_message(text: str) -> dict:
    token, chat_id = _get_creds()
    if not token or not chat_id:
        return {"ok": False, "error": "TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados. "
                "Define via: python3 bin/secrets.py set TELEGRAM_TOKEN <token>"}

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            return {"ok": resp.get("ok", False), "message_id": resp.get("result", {}).get("message_id")}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Formatters ────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def alert_worker_crash(job_id: str = "") -> dict:
    msg = (f"🔴 *AI-OS — Worker Crash*\n"
           f"O autopilot/worker parou inesperadamente.\n"
           f"Job: `{job_id or 'N/A'}`\n"
           f"_{_ts()}_")
    return send_message(msg)


def alert_job_failed(job_id: str, attempts: int = 1, error: str = "") -> dict:
    icon = "🔴" if attempts >= 3 else "🟡"
    msg  = (f"{icon} *AI-OS — Job Falhou*\n"
            f"Job: `{job_id}`\n"
            f"Tentativas: `{attempts}`\n")
    if error:
        msg += f"Erro: `{error[:200]}`\n"
    msg += f"_{_ts()}_"
    return send_message(msg)


def alert_db_down(detail: str = "") -> dict:
    msg = (f"🔴 *AI-OS — DB Inacessível*\n"
           f"PostgreSQL não responde.\n")
    if detail:
        msg += f"`{detail[:200]}`\n"
    msg += f"_{_ts()}_"
    return send_message(msg)


def alert_token_expired(user: str = "") -> dict:
    msg = (f"🟡 *AI-OS — Token Expirado*\n"
           f"Utilizador: `{user or 'desconhecido'}`\n"
           f"_{_ts()}_")
    return send_message(msg)


def alert_backup_failed(detail: str = "") -> dict:
    msg = (f"🔴 *AI-OS — Backup Falhou*\n"
           f"O backup Postgres não foi concluído.\n")
    if detail:
        msg += f"`{detail[:200]}`\n"
    msg += f"_{_ts()}_"
    return send_message(msg)


def alert_custom(message: str) -> dict:
    return send_message(f"ℹ️ *AI-OS*\n{message}\n_{_ts()}_")


# ── Autopilot integration helper ──────────────────────────────────────────────

def notify(trigger: str, **kwargs) -> dict:
    """
    Ponto único de entrada para todos os triggers.
    Usado pelo autopilot: alerting.notify('job_failed', job_id=..., attempts=3)
    """
    handlers = {
        "worker_crash":    lambda: alert_worker_crash(kwargs.get("job_id", "")),
        "job_failed":      lambda: alert_job_failed(
                               kwargs.get("job_id", "?"),
                               kwargs.get("attempts", 1),
                               kwargs.get("error", ""),
                           ),
        "db_down":         lambda: alert_db_down(kwargs.get("detail", "")),
        "token_expired":   lambda: alert_token_expired(kwargs.get("user", "")),
        "backup_failed":   lambda: alert_backup_failed(kwargs.get("detail", "")),
        "custom":          lambda: alert_custom(kwargs.get("msg", "")),
    }
    fn = handlers.get(trigger)
    if not fn:
        return {"ok": False, "error": f"trigger desconhecido: {trigger}"}
    return fn()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "test":
        print(json.dumps(alert_custom("Teste de alerta — sistema operacional ✅")))

    elif cmd == "send" and len(sys.argv) >= 3:
        trigger = sys.argv[2]
        msg     = sys.argv[3] if len(sys.argv) > 3 else ""
        print(json.dumps(notify(trigger, msg=msg, job_id=msg, detail=msg)))

    elif cmd == "worker_crash":
        print(json.dumps(alert_worker_crash(sys.argv[2] if len(sys.argv) > 2 else "")))

    elif cmd == "job_failed" and len(sys.argv) >= 3:
        job_id   = sys.argv[2]
        attempts = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        print(json.dumps(alert_job_failed(job_id, attempts)))

    elif cmd == "db_down":
        print(json.dumps(alert_db_down(sys.argv[2] if len(sys.argv) > 2 else "")))

    elif cmd == "token_expired":
        print(json.dumps(alert_token_expired(sys.argv[2] if len(sys.argv) > 2 else "")))

    elif cmd == "backup_failed":
        print(json.dumps(alert_backup_failed(sys.argv[2] if len(sys.argv) > 2 else "")))

    else:
        print(json.dumps({
            "ok": False,
            "error": "uso: test|send <trigger>|worker_crash|job_failed|db_down|token_expired|backup_failed",
            "setup": "python3 bin/secrets.py set TELEGRAM_TOKEN <token> && python3 bin/secrets.py set TELEGRAM_CHAT_ID <id>",
        }))
