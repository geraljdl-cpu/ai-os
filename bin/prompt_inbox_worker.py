#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
prompt_inbox_worker.py — Processa prompts pendentes em agent_inbox via Claude API.

Corre via systemd timer (a cada 30s) e em fire-and-forget após cada POST /api/agent-inbox.
"""

import datetime as dt
import json
import logging
import os
from pathlib import Path

# ── Load env ──────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()  # ficheiro tem prioridade

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL  = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)
MODEL         = "claude-haiku-4-5-20251001"
MAX_TOKENS    = 1024
MAX_BODY_CHARS = 3000   # Truncar prompts muito longos
BATCH_SIZE    = 5       # Itens por execução

# Targets que usam Ollama local em vez de Claude
LOCAL_TARGETS = {"local", "asus_gpu", "cluster_cpu"}

SYSTEM_PROMPT = """És um assistente pessoal de João Diogo Lopes, gerente da empresa JOAO DIOGO LOPES UNIP LDA (Lisboa, Portugal).

Contexto do sistema:
- AI-OS: plataforma de gestão operacional com stack Express/Node.js, Python, PostgreSQL, cluster de 7 nodes Raspberry Pi
- Módulos activos: serviços/timesheets, faturação, marketplace de trabalhadores, RH, seguros, viaturas, radar de tenders
- Língua: Português europeu (PT)

Regras de resposta:
- Sê conciso e directo. Sem floreados.
- Quando te pedem análise de negócio, foca em impacto prático e próximos passos concretos.
- Se não souberes algo ou precisares de mais contexto, diz-o claramente."""

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prompt_inbox] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger(__name__)


# ── Ollama helper ─────────────────────────────────────────────────────────────

def _call_ollama(url: str, model: str, system: str, prompt: str) -> str:
    """Chama Ollama API (compatível com OpenAI /v1/chat/completions)."""
    import urllib.request
    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "options": {"num_predict": MAX_TOKENS},
    }).encode()
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read())
    return result["message"]["content"]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


# ── Core processing ───────────────────────────────────────────────────────────

def process_pending() -> dict:
    """Processa até BATCH_SIZE itens pending de agent_inbox. Devolve stats."""
    import anthropic
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

    # Import model_router para seleccionar endpoint Ollama
    import importlib.util, pathlib
    _mr_path = pathlib.Path(__file__).parent / "model_router.py"
    _spec = importlib.util.spec_from_file_location("model_router", _mr_path)
    _mr = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mr)

    engine, text = _conn()
    processed = done = errors = 0

    with engine.begin() as conn:
        # Buscar pending com lock — claude E local targets
        rows = conn.execute(text("""
            SELECT id, body, source, sender, target
            FROM public.agent_inbox
            WHERE status = 'pending' AND target IN ('claude','local','asus_gpu','cluster_cpu')
            ORDER BY created_at ASC
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        """), {"limit": BATCH_SIZE}).fetchall()

        if not rows:
            log.debug("Nenhum item pending")
            return {"processed": 0}

        # Marcar todos como 'sent' antes de chamar API (lock optimista)
        ids = [r[0] for r in rows]
        conn.execute(text("""
            UPDATE public.agent_inbox
            SET status = 'sent', updated_at = NOW()
            WHERE id = ANY(:ids)
        """), {"ids": ids})

    # Processar cada item (fora da transação de lock)
    for row in rows:
        item_id, body, source, sender, target = row[0], row[1], row[2], row[3], row[4]
        processed += 1
        log.info(f"Processando item #{item_id} (source={source}, target={target})")

        try:
            prompt = str(body)[:MAX_BODY_CHARS]

            if target in LOCAL_TARGETS:
                # Ollama local (asus_gpu → cluster_cpu → fallback claude)
                tier = 1 if target == "asus_gpu" else (2 if target == "cluster_cpu" else None)
                providers = _mr.get_healthy_providers(tier) if tier else (
                    _mr.get_healthy_providers(1) or _mr.get_healthy_providers(2)
                )
                if providers:
                    p = providers[0]
                    result_text = _call_ollama(p["url"], p["model"], SYSTEM_PROMPT, prompt)
                    log.info(f"Item #{item_id} via Ollama {p['name']} ({len(result_text)} chars)")
                elif claude_client:
                    log.warning(f"Item #{item_id}: Ollama indisponível, fallback Claude")
                    msg = claude_client.messages.create(
                        model=MODEL, max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    result_text = msg.content[0].text
                else:
                    raise RuntimeError("Nenhum provider disponível (Ollama down, sem chave Claude)")
            else:
                # Claude directo
                if not claude_client:
                    raise RuntimeError("ANTHROPIC_API_KEY não configurada")
                msg = claude_client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}]
                )
                result_text = msg.content[0].text
            log.info(f"Item #{item_id} processado ({len(result_text)} chars)")

            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE public.agent_inbox
                    SET status = 'done', result = :result, updated_at = NOW()
                    WHERE id = :id
                """), {"result": result_text, "id": item_id})
            done += 1

        except Exception as e:
            log.error(f"Erro ao processar item #{item_id}: {e}")
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE public.agent_inbox
                    SET status = 'error', result = :result, updated_at = NOW()
                    WHERE id = :id
                """), {"result": str(e)[:500], "id": item_id})
            errors += 1

    log.info(f"Batch concluído: {done} done, {errors} errors, {processed} total")
    return {"processed": processed, "done": done, "errors": errors}


if __name__ == "__main__":
    result = process_pending()
    print(json.dumps(result))
