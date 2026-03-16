#!/usr/bin/env python3
"""
council.py — AI Council Router
Generic multi-agent analysis for any topic: idea, decision, project, architecture, problem.

Usage:
  python3 bin/council.py analyze "<topic text>" [--kind idea|decision|project|architecture|problem|general] [--ref-id X]
  python3 bin/council.py get <council_id>       — get full analysis
  python3 bin/council.py list [--kind K] [--limit N]

Each agent returns structured analysis; synthesis generates a final recommendation.
Results stored in council_reviews table.
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, json, os, re, sys

import anthropic
import sqlalchemy as sa

DATABASE_URL  = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL         = "claude-haiku-4-5-20251001"   # fast + cheap for council calls

AGENTS = ["strategist", "engineering", "operations", "finance"]

AGENT_PROMPTS = {
    "strategist": """\
És o AI Strategist do AI-OS Council.
Analisa qualquer tópico do ponto de vista estratégico: visão, posicionamento, riscos, timing.

Responde em JSON exacto:
{"analysis":"...","risks":"...","opportunity":"...","score":0,"recommendation":"..."}

- analysis: 2-3 frases sobre o tópico e potencial estratégico
- risks: principais riscos estratégicos (1 parágrafo)
- opportunity: oportunidade ou vantagem principal
- score: 0-100 (potencial estratégico)
- recommendation: explorar | executar | rejeitar | investigar + 1 frase""",

    "engineering": """\
És o AI Engineering do AI-OS Council.
Analisa qualquer tópico do ponto de vista técnico: viabilidade, stack, complexidade, esforço.

Responde em JSON exacto:
{"analysis":"...","risks":"...","opportunity":"...","score":0,"recommendation":"..."}

- analysis: 2-3 frases sobre viabilidade técnica
- risks: riscos técnicos principais
- opportunity: vantagem ou abordagem técnica recomendada
- score: 0-100 (viabilidade técnica)
- recommendation: explorar | executar | rejeitar | investigar + 1 frase""",

    "operations": """\
És o AI Operations do AI-OS Council.
Analisa qualquer tópico do ponto de vista operacional: execução, recursos, timeline, dependências.

Responde em JSON exacto:
{"analysis":"...","risks":"...","opportunity":"...","score":0,"recommendation":"..."}

- analysis: 2-3 frases sobre como executar
- risks: riscos operacionais principais
- opportunity: como simplificar ou acelerar execução
- score: 0-100 (exequibilidade)
- recommendation: explorar | executar | rejeitar | investigar + 1 frase""",

    "finance": """\
És o AI Finance do AI-OS Council.
Analisa qualquer tópico do ponto de vista financeiro: custos, ROI, cash-flow, viabilidade económica.

Responde em JSON exacto:
{"analysis":"...","risks":"...","opportunity":"...","score":0,"recommendation":"..."}

- analysis: 2-3 frases sobre impacto financeiro
- risks: riscos financeiros principais
- opportunity: potencial de retorno ou poupança
- score: 0-100 (viabilidade financeira)
- recommendation: explorar | executar | rejeitar | investigar + 1 frase""",
}


def _conn():
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _call_claude(system: str, user: str) -> str:
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não definida")
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model=MODEL, max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _parse_agent_response(raw: str) -> dict:
    """Extract JSON from agent response, with fallback."""
    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"analysis": raw[:300], "risks": "", "opportunity": "", "score": None, "recommendation": "investigar"}


def _synthesize(topic: str, agent_results: list) -> dict:
    scores = [r["score"] for r in agent_results if r.get("score") is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else None

    # Determine consensus
    recs = [r.get("recommendation", "").split()[0].lower() for r in agent_results]
    rec_counts = {}
    for r in recs:
        rec_counts[r] = rec_counts.get(r, 0) + 1
    top_rec = max(rec_counts, key=rec_counts.get) if rec_counts else "investigar"
    consensus = "consenso" if rec_counts.get(top_rec, 0) >= 3 else "divergência"

    # Decision mapping
    decision_map = {
        "executar": "executar",
        "explorar": "explorar",
        "rejeitar": "rejeitar",
        "investigar": "investigar mais",
    }
    decision = decision_map.get(top_rec, "investigar mais")

    parts = [f"Tópico: {topic}", ""]
    for r in agent_results:
        parts.append(f"{r['agent'].upper()}: score {r.get('score','?')} — {r.get('recommendation','?')}")
        parts.append(f"  {r.get('analysis','')[:120]}")

    synthesis_text = "\n".join(parts)

    return {
        "avg_score":  avg_score,
        "consensus":  consensus,
        "decision":   decision,
        "top_rec":    top_rec,
        "synthesis":  synthesis_text,
        "agents":     agent_results,
    }


def cmd_analyze(topic: str, kind: str = "general", ref_id: str | None = None) -> dict:
    """Run all 4 agents + synthesis. Returns full result dict."""
    engine, text = _conn()

    agent_results = []
    session_id = None  # track first insert for grouping

    with engine.begin() as c:
        for agent_name in AGENTS:
            print(f"[council] {agent_name}...", file=sys.stderr)
            system = AGENT_PROMPTS[agent_name]
            try:
                raw    = _call_claude(system, f"Analisa este tópico:\n\n{topic}")
                parsed = _parse_agent_response(raw)
                parsed["raw"] = raw
            except Exception as e:
                parsed = {"analysis": f"Erro: {e}", "risks": "", "opportunity": "",
                          "score": None, "recommendation": "investigar", "raw": str(e)}

            parsed["agent"] = agent_name
            agent_results.append(parsed)

            row_id = c.execute(text("""
                INSERT INTO public.council_reviews
                  (topic, topic_kind, ref_id, agent, analysis, risks, opportunity, score, recommendation, raw)
                VALUES (:topic, :kind, :ref_id, :agent, :analysis, :risks, :opportunity, :score, :recommendation, :raw)
                RETURNING id
            """), {
                "topic":          topic[:500],
                "kind":           kind,
                "ref_id":         ref_id,
                "agent":          agent_name,
                "analysis":       parsed.get("analysis", "")[:2000],
                "risks":          parsed.get("risks", "")[:1000],
                "opportunity":    parsed.get("opportunity", "")[:1000],
                "score":          parsed.get("score"),
                "recommendation": (parsed.get("recommendation") or "")[:200],
                "raw":            (parsed.get("raw") or "")[:4000],
            }).scalar()
            if session_id is None:
                session_id = row_id

        synthesis = _synthesize(topic, agent_results)

        # Extract next_steps as bullet points from agent recommendations
        next_steps = []
        for r in agent_results:
            rec = (r.get("recommendation") or "").strip()
            opp = (r.get("opportunity") or "").strip()
            if rec:
                next_steps.append(f"[{r['agent'].upper()}] {rec}: {opp[:120]}" if opp else f"[{r['agent'].upper()}] {rec}")
        suggested_tasks = [
            {"agent": r["agent"], "action": (r.get("recommendation") or "").split()[0], "detail": (r.get("analysis") or "")[:200]}
            for r in agent_results
        ]

        # Store synthesis as 'system' agent row
        c.execute(text("""
            INSERT INTO public.council_reviews
              (topic, topic_kind, ref_id, agent, analysis, risks, opportunity, score, recommendation, raw,
               synthesis, next_steps, suggested_tasks, status)
            VALUES (:topic, :kind, :ref_id, 'system', :analysis, '', '', :score, :recommendation, :raw,
                    :synthesis, CAST(:next_steps AS jsonb), CAST(:suggested_tasks AS jsonb), 'done')
        """), {
            "topic":          topic[:500],
            "kind":           kind,
            "ref_id":         ref_id,
            "analysis":       synthesis["synthesis"][:2000],
            "score":          synthesis["avg_score"],
            "recommendation": synthesis["decision"],
            "raw":            json.dumps(synthesis),
            "synthesis":      synthesis["synthesis"][:2000],
            "next_steps":     json.dumps(next_steps),
            "suggested_tasks": json.dumps(suggested_tasks),
        })

        # Event
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'council', 'council_analyzed', :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Council: {topic[:80]} — {synthesis['decision']} (score {synthesis['avg_score']})",
            "data": json.dumps({"kind": kind, "decision": synthesis["decision"],
                                "avg_score": synthesis["avg_score"], "session_id": session_id}),
        })

    return {
        "session_id":  session_id,
        "topic":       topic,
        "kind":        kind,
        "avg_score":   synthesis["avg_score"],
        "consensus":   synthesis["consensus"],
        "decision":    synthesis["decision"],
        "agents":      [{"agent": r["agent"], "score": r.get("score"),
                         "recommendation": r.get("recommendation"),
                         "analysis": (r.get("analysis") or "")[:200]} for r in agent_results],
    }


def cmd_list(kind: str | None = None, limit: int = 10) -> list:
    engine, text = _conn()
    kind_filter = "AND topic_kind = :kind" if kind else ""
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT DISTINCT ON (topic) topic, topic_kind, score, recommendation, created_at
            FROM public.council_reviews
            WHERE agent = 'system' {kind_filter}
            ORDER BY topic, created_at DESC
            LIMIT :limit
        """), {"kind": kind, "limit": limit}).mappings().all()
    return [dict(r) for r in rows]


def cmd_get(session_id: int) -> dict:
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT agent, analysis, risks, opportunity, score, recommendation, created_at
            FROM public.council_reviews
            WHERE id >= :sid AND topic = (SELECT topic FROM public.council_reviews WHERE id = :sid)
              AND created_at >= (SELECT created_at FROM public.council_reviews WHERE id = :sid) - interval '1 minute'
            ORDER BY created_at
        """), {"sid": session_id}).mappings().all()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub    = parser.add_subparsers(dest="cmd")

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("topic")
    p_analyze.add_argument("--kind", default="general",
                           choices=["idea","decision","project","architecture","problem","general"])
    p_analyze.add_argument("--ref-id", default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=10)

    p_get = sub.add_parser("get")
    p_get.add_argument("session_id", type=int)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    if args.cmd == "analyze":
        result = cmd_analyze(args.topic, args.kind, getattr(args, "ref_id", None))
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.cmd == "list":
        items = cmd_list(args.kind, args.limit)
        print(json.dumps(items, ensure_ascii=False, default=str))

    elif args.cmd == "get":
        rows = cmd_get(args.session_id)
        print(json.dumps(rows, ensure_ascii=False, default=str))
