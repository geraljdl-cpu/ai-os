#!/usr/bin/env python3
"""
idea_router.py — Conselho de IA (Sprint I)
Analisa uma ideia com 4 agentes Claude:
  strategist  — visão, mercado, posicionamento, risco estratégico
  engineering — viabilidade técnica, stack, arquitetura, esforço
  operations  — execução, equipa, recursos, prazo, dependências
  finance     — custos, receita estimada, ROI, cash-flow

Uso:
  python3 bin/idea_router.py <thread_id>
  python3 bin/idea_router.py <thread_id> [strategist|engineering|operations|finance]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import re
import sys

import anthropic
import sqlalchemy as sa

DATABASE_URL   = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL          = "claude-sonnet-4-6"

# ── Prompts por papel ─────────────────────────────────────────────────────────

SYSTEM_PROMPTS = {
    "strategist": """\
És o AI Strategist do Conselho de IA do AI-OS.
O teu papel: analisar ideias do ponto de vista estratégico.
Foca em: visão de longo prazo, mercado-alvo, posicionamento competitivo,
timing, riscos estratégicos, e alavancagem da ideia.

Responde SEMPRE neste formato exato:

Resumo:
<2-3 frases sobre a ideia e o seu potencial estratégico>

Riscos:
<bullet points dos 3 principais riscos estratégicos>

Próximos passos:
<3 ações concretas para validar ou avançar>

Score: <número 0-100>
""",
    "engineering": """\
És o AI Engineering do Conselho de IA do AI-OS.
O teu papel: analisar a viabilidade técnica de ideias.
Foca em: stack tecnológico, arquitetura, complexidade de implementação,
integrações necessárias, esforço estimado, e dívida técnica.

Responde SEMPRE neste formato exato:

Resumo:
<2-3 frases sobre viabilidade e abordagem técnica>

Riscos:
<bullet points dos 3 principais riscos técnicos>

Próximos passos:
<3 ações técnicas concretas: proof of concept, protótipo, decisões de arquitetura>

Score: <número 0-100 de viabilidade técnica>
""",
    "operations": """\
És o AI Operations do Conselho de IA do AI-OS.
O teu papel: analisar como executar e operar a ideia.
Foca em: equipa necessária, recursos, timeline realista,
dependências externas, processos operacionais, e sustentabilidade.

Responde SEMPRE neste formato exato:

Resumo:
<2-3 frases sobre como executar e operar>

Riscos:
<bullet points dos 3 principais riscos operacionais>

Próximos passos:
<3 ações operacionais: quem faz o quê, quando, com que recursos>

Score: <número 0-100 de exequibilidade operacional>
""",
    "finance": """\
És o AI Finance do Conselho de IA do AI-OS.
O teu papel: analisar o impacto financeiro e viabilidade económica de ideias.
Foca em: custos de implementação, custos operacionais, receita potencial,
ROI estimado, tempo até break-even, e fluxo de caixa.

Responde SEMPRE neste formato exato:

Resumo:
<2-3 frases sobre viabilidade financeira e potencial de retorno>

Riscos:
<bullet points dos 3 principais riscos financeiros>

Próximos passos:
<3 ações financeiras: estimar custos reais, validar pricing, calcular break-even>

Score: <número 0-100 de viabilidade financeira>
""",
}

SYNTHESIS_PROMPT = """\
És o coordenador do Conselho de IA do AI-OS.
Recebeste as análises de 4 especialistas sobre uma ideia.
Faz uma síntese executiva para o decisor.

Responde neste formato:

Decisão sugerida:
<aprovado para avançar | explorar mais | aguardar | arquivar — com 1 frase de justificação>

Prioridade:
<alta | média | baixa — com razão>

Próximos 3 passos:
<lista numerada de 3 ações concretas por ordem de prioridade>

Score médio: <média aritmética dos 4 scores>
"""


def _conn():
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _parse_review(raw: str) -> dict:
    """Extrai campos estruturados de uma resposta de agente."""
    def _extract(key):
        pattern = rf"{key}:\s*\n([\s\S]*?)(?=\n\n[A-ZÁÉÍÓÚÇÂÊÔÀÜ]|\nScore:|\Z)"
        m = re.search(pattern, raw, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    score_m = re.search(r"Score:\s*(\d+)", raw, re.IGNORECASE)
    return {
        "summary":    _extract("Resumo"),
        "risks":      _extract("Riscos"),
        "next_steps": _extract(r"Próximos passos"),
        "score":      int(score_m.group(1)) if score_m else None,
        "raw":        raw,
    }


def _call_claude(system: str, user: str) -> str:
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não definida. Adicionar a /etc/aios.env")
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def analyze_thread(thread_id: int, agents: list = None) -> list:
    """Analisa um thread com os agentes especificados (ou todos)."""
    engine, text = _conn()
    agents = agents or list(SYSTEM_PROMPTS.keys())

    with engine.connect() as c:
        thread = c.execute(text(
            "SELECT id, title FROM public.idea_threads WHERE id = :id"
        ), {"id": thread_id}).mappings().first()
        if not thread:
            raise ValueError(f"Thread {thread_id} não encontrado")

        # última mensagem do utilizador
        msgs = c.execute(text("""
            SELECT role, content FROM public.idea_messages
            WHERE thread_id = :tid
            ORDER BY created_at DESC
            LIMIT 10
        """), {"tid": thread_id}).mappings().all()

    if not msgs:
        raise ValueError(f"Thread {thread_id} sem mensagens")

    # contexto para os agentes
    context_parts = [f"Ideia: {thread['title']}"]
    for m in reversed(msgs):
        if m["role"] in ("joao", "user"):
            context_parts.append(f"João: {m['content']}")
        elif m["role"] != "system":
            context_parts.append(f"[{m['role']}]: {m['content']}")
    user_context = "\n\n".join(context_parts)

    results = []
    with engine.begin() as c:
        for agent in agents:
            print(f"[idea_router] agente: {agent}...", file=sys.stderr)
            system = SYSTEM_PROMPTS[agent]
            raw    = _call_claude(system, user_context)
            parsed = _parse_review(raw)

            # gravar na DB
            c.execute(text("""
                INSERT INTO public.idea_reviews
                  (thread_id, agent, summary, risks, next_steps, score, raw)
                VALUES (:tid, :agent, :summary, :risks, :next_steps, :score, :raw)
            """), {
                "tid":        thread_id,
                "agent":      agent,
                "summary":    parsed["summary"],
                "risks":      parsed["risks"],
                "next_steps": parsed["next_steps"],
                "score":      parsed["score"],
                "raw":        parsed["raw"],
            })

            # evento
            c.execute(text("""
                INSERT INTO public.events (ts, level, source, kind, message, data)
                VALUES (NOW(), 'info', 'idea_router', 'idea_analyzed',
                        :msg, CAST(:data AS jsonb))
            """), {
                "msg":  f"Ideia {thread_id} analisada por {agent} (score {parsed['score']})",
                "data": json.dumps({"thread_id": thread_id, "agent": agent, "score": parsed["score"]})
            })

            results.append({"agent": agent, **parsed})

        # marcar thread como analisado
        c.execute(text("""
            UPDATE public.idea_threads
            SET status = 'analyzed', updated_at = NOW()
            WHERE id = :id
        """), {"id": thread_id})

    return results


def synthesize_thread(thread_id: int) -> dict:
    """Cria síntese executiva a partir das reviews existentes."""
    engine, text = _conn()
    with engine.connect() as c:
        thread = c.execute(text(
            "SELECT id, title FROM public.idea_threads WHERE id = :id"
        ), {"id": thread_id}).mappings().first()
        reviews = c.execute(text("""
            SELECT agent, summary, risks, next_steps, score
            FROM public.idea_reviews
            WHERE thread_id = :tid AND agent != 'system'
            ORDER BY created_at
        """), {"tid": thread_id}).mappings().all()

    if not reviews:
        raise ValueError("Sem reviews para sintetizar")

    parts = [f"Ideia: {thread['title']}\n"]
    for r in reviews:
        parts.append(f"### {r['agent'].upper()}\nResumo: {r['summary']}\nRiscos: {r['risks']}\nPróximos passos: {r['next_steps']}\nScore: {r['score']}")
    synthesis_input = "\n\n".join(parts)

    print("[idea_router] síntese...", file=sys.stderr)
    raw    = _call_claude(SYNTHESIS_PROMPT, synthesis_input)
    parsed = _parse_review(raw)
    parsed["raw"]   = raw
    parsed["agent"] = "system"

    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO public.idea_reviews
              (thread_id, agent, summary, risks, next_steps, score, raw)
            VALUES (:tid, 'system', :summary, :risks, :next_steps, :score, :raw)
        """), {
            "tid":        thread_id,
            "summary":    raw,          # guardar texto completo em summary para síntese
            "risks":      parsed["risks"],
            "next_steps": parsed["next_steps"],
            "score":      parsed["score"],
            "raw":        raw,
        })

    return {"agent": "system", "synthesis": raw, **parsed}


def main():
    if len(sys.argv) < 2:
        print("uso: idea_router.py <thread_id> [agent...]", file=sys.stderr)
        sys.exit(1)

    thread_id = int(sys.argv[1])
    agents    = sys.argv[2:] if len(sys.argv) > 2 else None

    results = analyze_thread(thread_id, agents)

    # síntese automática se todos os 4 agentes correram
    ran_agents = {r["agent"] for r in results}
    if not agents or set(SYSTEM_PROMPTS.keys()).issubset(ran_agents):
        synthesis = synthesize_thread(thread_id)
        results.append(synthesis)

    print(json.dumps({"ok": True, "thread_id": thread_id, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
