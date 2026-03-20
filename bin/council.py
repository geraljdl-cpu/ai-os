#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
council.py — AI Council: 3 agentes paralelos (engineer, architect, reviewer)

Usage:
  python3 bin/council.py analyze "<topic>" [--kind idea|decision|project|general] [--ref-id X] [--context "..."]
  python3 bin/council.py get <id>
  python3 bin/council.py list [--kind K] [--limit N]
"""

import argparse, json, os, re, sys
from decimal import Decimal

_env_file = "/etc/aios.env"
_env_vals: dict = {}
if os.path.exists(_env_file):
    for _l in open(_env_file):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _, _v = _l.partition("=")
            _env_vals[_k.strip()] = _v.strip()
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
import sqlalchemy as sa

DATABASE_URL  = _env_vals.get("DATABASE_URL") or os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
# Read ANTHROPIC_API_KEY directly from file — shell env may have a masked value
ANTHROPIC_KEY = _env_vals.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
MODEL         = "claude-haiku-4-5-20251001"

# ── Agentes ───────────────────────────────────────────────────────────────────

AGENTS = {
    "engineer": """\
És o AI Engineer do Council.
Analisa viabilidade técnica, complexidade de implementação, stack necessária, riscos técnicos e esforço estimado.

Responde APENAS em JSON válido:
{"analysis":"...","risks":"...","recommendation":"executar|explorar|investigar|rejeitar","next_steps":["...","..."],"score":75}

- analysis: 2-3 frases sobre viabilidade técnica
- risks: principais riscos técnicos (1-2 frases)
- recommendation: uma palavra: executar | explorar | investigar | rejeitar
- next_steps: lista de 2-3 passos técnicos concretos
- score: 0-100 (viabilidade técnica)""",

    "architect": """\
És o AI Architect do Council.
Analisa arquitectura, padrões de design, escalabilidade, alinhamento com sistema existente e maintainability.

Responde APENAS em JSON válido:
{"analysis":"...","risks":"...","recommendation":"executar|explorar|investigar|rejeitar","next_steps":["...","..."],"score":75}

- analysis: 2-3 frases sobre a arquitectura recomendada
- risks: riscos de design ou dívida técnica (1-2 frases)
- recommendation: uma palavra: executar | explorar | investigar | rejeitar
- next_steps: lista de 2-3 decisões arquitecturais concretas
- score: 0-100 (qualidade arquitectural)""",

    "reviewer": """\
És o AI Reviewer do Council. Tens acesso às análises dos outros agentes.
Faz review crítico, identifica inconsistências, valida as recomendações e dá veredicto final.

Responde APENAS em JSON válido:
{"analysis":"...","risks":"...","recommendation":"executar|explorar|investigar|rejeitar","next_steps":["...","..."],"suggested_tasks":["...","..."],"score":75}

- analysis: síntese crítica com veredicto final (3-4 frases)
- risks: riscos mais críticos identificados
- recommendation: veredicto final: executar | explorar | investigar | rejeitar
- next_steps: 3-5 próximos passos prioritários
- suggested_tasks: 2-4 tarefas concretas a criar no sistema
- score: 0-100 (score final consolidado)""",
}

# ── DB ────────────────────────────────────────────────────────────────────────

def _conn():
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _row(r) -> dict:
    import datetime as _dt
    d = dict(r._mapping) if hasattr(r, '_mapping') else dict(r)
    for k, v in list(d.items()):
        if isinstance(v, (_dt.datetime, _dt.date)):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    return d


# ── Claude call ───────────────────────────────────────────────────────────────

def _call_agent(agent_name: str, prompt: str, topic: str, context: str = "",
                max_tokens: int = 700) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    user_msg = f"Tópico para análise:\n\n{topic}"
    if context:
        user_msg += f"\n\nContexto adicional:\n{context}"
    print(f"[council] {agent_name}...", file=sys.stderr)
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=max_tokens,
            system=prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = msg.content[0].text
        # Try to extract JSON — handles plain JSON, ```json blocks, and mixed text
        parsed = None
        # First try: direct parse
        try:
            parsed = json.loads(raw.strip())
        except Exception:
            pass
        if not parsed:
            # Extract JSON between first { and last } — handles ```json blocks
            start = raw.find('{')
            end = raw.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(raw[start:end+1])
                except Exception:
                    pass
        if not parsed:
            parsed = {}
        parsed.setdefault("analysis", raw[:400])
        parsed.setdefault("risks", "")
        parsed.setdefault("recommendation", "investigar")
        parsed.setdefault("next_steps", [])
        parsed.setdefault("score", None)
        return parsed
    except Exception as e:
        return {"analysis": f"Erro: {e}", "risks": "", "recommendation": "investigar",
                "next_steps": [], "score": None}


# ── Council analyze ───────────────────────────────────────────────────────────

def cmd_analyze(topic: str, kind: str = "general", ref_id: str | None = None,
                context: str = "") -> dict:
    if not ANTHROPIC_KEY:
        return {"error": "ANTHROPIC_API_KEY não definida"}

    # Run engineer + architect first, then reviewer with their context
    eng  = _call_agent("engineer",  AGENTS["engineer"],  topic, context)
    arch = _call_agent("architect", AGENTS["architect"], topic, context)

    reviewer_context = (
        f"{context}\n\n" if context else ""
    ) + (
        f"ENGINEER: score={eng.get('score')} rec={eng.get('recommendation')}\n"
        f"{eng.get('analysis','')}\n\n"
        f"ARCHITECT: score={arch.get('score')} rec={arch.get('recommendation')}\n"
        f"{arch.get('analysis','')}"
    )
    rev = _call_agent("reviewer", AGENTS["reviewer"], topic, reviewer_context, max_tokens=1200)

    agents_json = {
        "engineer":  {k: eng.get(k)  for k in ("analysis","risks","recommendation","next_steps","score")},
        "architect": {k: arch.get(k) for k in ("analysis","risks","recommendation","next_steps","score")},
        "reviewer":  {k: rev.get(k)  for k in ("analysis","risks","recommendation","next_steps","suggested_tasks","score")},
    }

    # Synthesis
    scores = [v["score"] for v in agents_json.values() if v.get("score") is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else None
    final_rec = rev.get("recommendation", "investigar")

    synthesis = (
        f"**Veredicto: {final_rec.upper()}** (score médio: {avg_score}/100)\n\n"
        f"{rev.get('analysis','')}\n\n"
        f"Engineer ({eng.get('score','?')}): {eng.get('recommendation','?')} — {eng.get('analysis','')[:120]}\n"
        f"Architect ({arch.get('score','?')}): {arch.get('recommendation','?')} — {arch.get('analysis','')[:120]}"
    )

    next_steps       = rev.get("next_steps", []) or []
    suggested_tasks  = rev.get("suggested_tasks", []) or []

    engine, text = _conn()
    with engine.begin() as c:
        row_id = c.execute(text("""
            INSERT INTO public.council_reviews
              (topic, context, agents, synthesis, next_steps, suggested_tasks, status)
            VALUES (:topic, :context, CAST(:agents AS jsonb),
                    :synthesis,
                    CAST(:next_steps AS jsonb), CAST(:suggested_tasks AS jsonb),
                    'done')
            RETURNING id
        """), {
            "topic":          topic[:500],
            "context":        (context or "")[:1000],
            "agents":         json.dumps(agents_json),
            "synthesis":      synthesis[:3000],
            "next_steps":     json.dumps(next_steps),
            "suggested_tasks": json.dumps(suggested_tasks),
        }).scalar()

        # Event
        try:
            c.execute(text("""
                INSERT INTO public.events (ts, level, source, kind, message, data)
                VALUES (NOW(), 'info', 'council', 'council_analyzed', :msg, CAST(:data AS jsonb))
            """), {
                "msg":  f"Council: {topic[:80]} — {final_rec} (score {avg_score})",
                "data": json.dumps({"id": row_id, "kind": kind, "ref_id": ref_id,
                                    "decision": final_rec, "avg_score": avg_score}),
            })
        except Exception:
            pass

    return {
        "ok":             True,
        "id":             row_id,
        "topic":          topic,
        "kind":           kind,
        "avg_score":      avg_score,
        "recommendation": final_rec,
        "synthesis":      synthesis,
        "next_steps":     next_steps,
        "suggested_tasks": suggested_tasks,
        "agents": {
            name: {"score": d.get("score"), "recommendation": d.get("recommendation"),
                   "analysis": (d.get("analysis") or "")[:200]}
            for name, d in agents_json.items()
        },
    }


def cmd_get(review_id: int) -> dict:
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT * FROM public.council_reviews WHERE id = :id"
        ), {"id": review_id}).mappings().first()
    return _row(row) if row else {}


def cmd_list(kind: str | None = None, limit: int = 10) -> list:
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, topic, synthesis, status, created_at
            FROM public.council_reviews
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit}).mappings().all()
    return [_row(r) for r in rows]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub    = parser.add_subparsers(dest="cmd")

    p_a = sub.add_parser("analyze")
    p_a.add_argument("topic")
    p_a.add_argument("--kind", default="general",
                     choices=["idea","decision","project","architecture","problem","general"])
    p_a.add_argument("--ref-id", default=None)
    p_a.add_argument("--context", default="")

    p_l = sub.add_parser("list")
    p_l.add_argument("--kind", default=None)
    p_l.add_argument("--limit", type=int, default=10)

    p_g = sub.add_parser("get")
    p_g.add_argument("id", type=int)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    if args.cmd == "analyze":
        r = cmd_analyze(args.topic, args.kind,
                        getattr(args, "ref_id", None), getattr(args, "context", ""))
        print(json.dumps(r, ensure_ascii=False, default=str))
    elif args.cmd == "list":
        print(json.dumps(cmd_list(args.kind, args.limit), ensure_ascii=False, default=str))
    elif args.cmd == "get":
        print(json.dumps(cmd_get(args.id), ensure_ascii=False, default=str))
