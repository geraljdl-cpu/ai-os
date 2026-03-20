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


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


# ── Core processing ───────────────────────────────────────────────────────────

def process_pending() -> dict:
    """Processa até BATCH_SIZE itens pending de agent_inbox. Devolve stats."""
    if not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY não configurada — a sair")
        return {"processed": 0, "error": "no api key"}

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    engine, text = _conn()
    processed = done = errors = 0

    with engine.begin() as conn:
        # Buscar pending com lock (evita double-processing)
        rows = conn.execute(text("""
            SELECT id, body, source, sender
            FROM public.agent_inbox
            WHERE status = 'pending' AND target = 'claude'
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
        item_id, body, source, sender = row[0], row[1], row[2], row[3]
        processed += 1
        log.info(f"Processando item #{item_id} (source={source}, sender={sender})")

        try:
            # Truncar prompt se necessário
            prompt = str(body)[:MAX_BODY_CHARS]

            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
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
