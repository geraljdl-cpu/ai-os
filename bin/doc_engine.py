#!/usr/bin/env python3
"""
doc_engine.py — Gerador de documentos PDF para AI-OS Twin

Uso:
  python3 doc_engine.py guia <entity_id>    — Guia do Lote
  python3 doc_engine.py fatura <entity_id>  — Fatura de Serviço

Output: JSON com {ok, path, filename}
"""
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

AIOS_ROOT  = Path(os.environ.get("AIOS_ROOT", Path.home() / "ai-os"))
DOCS_DIR   = AIOS_ROOT / "runtime" / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")

ESTADOS_PT = {
    "agendado": "Agendado", "chegou": "Chegou à fábrica",
    "em_processamento": "Em processamento", "separacao": "Separação cobre/plástico",
    "concluido": "Concluído", "pronto_levantar": "Pronto a levantar",
    "faturado": "Faturado", "fechado": "Fechado",
}

def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text

def _get_batch(eid: int) -> dict:
    engine, text = _conn()
    with engine.connect() as c:
        entity = c.execute(text(
            "SELECT id, name, metadata, created_at FROM public.twin_entities WHERE id=:id"
        ), {"id": eid}).mappings().first()
        if not entity:
            return {}
        case = c.execute(text(
            "SELECT id, status, data, created_at FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        tasks = []
        if case:
            tasks = c.execute(text(
                "SELECT title, type, status, updated_at FROM public.twin_tasks WHERE case_id=:cid ORDER BY id ASC"
            ), {"cid": case["id"]}).mappings().all()
        events = c.execute(text(
            "SELECT ts, kind, message FROM public.events WHERE entity_id=:eid ORDER BY ts ASC"
        ), {"eid": eid}).mappings().all()
    meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
    return {
        "entity_id": entity["id"],
        "name": entity["name"],
        "created_at": entity["created_at"].isoformat() if entity["created_at"] else "",
        "meta": meta,
        "case": dict(case) if case else {},
        "tasks": [dict(t) for t in tasks],
        "events": [dict(e) for e in events],
    }

def _nowstr():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _s(text) -> str:
    """Sanitize text for fpdf latin-1 output."""
    return (str(text)
            .replace("€", "EUR")
            .replace("→", "->")
            .replace("✓", "OK")
            .replace("○", "-")
            .replace("✅", "OK")
            .replace("❌", "X")
            .encode("latin-1", errors="replace")
            .decode("latin-1"))

def _pdf_header(pdf, title, subtitle=""):
    pdf.set_fill_color(11, 15, 30)
    pdf.rect(0, 0, 210, 30, 'F')
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(10, 8)
    pdf.cell(20, 10, "AI-OS")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_xy(35, 10)
    pdf.cell(0, 8, _s(title))
    if subtitle:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_xy(35, 19)
        pdf.set_text_color(160, 180, 220)
        pdf.cell(0, 6, _s(subtitle))
    pdf.set_text_color(30, 30, 30)
    pdf.set_xy(10, 35)

def _pdf_section(pdf, label):
    pdf.set_fill_color(240, 244, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 60, 130)
    pdf.cell(0, 7, _s(label), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "", 10)
    pdf.ln(1)

def _pdf_row(pdf, label, value, bold_value=False):
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 110, 130)
    pdf.cell(55, 6, _s(label) + ":")
    pdf.set_font("Helvetica", "B" if bold_value else "", 10)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 6, _s(value), new_x="LMARGIN", new_y="NEXT")


def generate_guia(entity_id: int) -> dict:
    """Gera PDF 'Guia do Lote'."""
    from fpdf import FPDF
    data = _get_batch(entity_id)
    if not data:
        return {"ok": False, "error": f"lote #{entity_id} não encontrado"}

    meta    = data["meta"]
    estado  = ESTADOS_PT.get(meta.get("estado", ""), meta.get("estado", "?"))
    client  = meta.get("client", "—")
    kg      = meta.get("kg", "—")
    now_str = _nowstr()
    fname   = f"guia_lote_{entity_id}.pdf"
    fpath   = DOCS_DIR / fname

    pdf = FPDF()
    pdf.add_page()
    _pdf_header(pdf, f"Guia do Lote #{entity_id}", f"Emitido: {now_str}")

    _pdf_section(pdf, "IDENTIFICAÇÃO DO LOTE")
    _pdf_row(pdf, "Nº do Lote",  f"#{entity_id}")
    _pdf_row(pdf, "Cliente",      client)
    _pdf_row(pdf, "Peso entrada", f"{kg} kg")
    _pdf_row(pdf, "Estado atual", estado, bold_value=True)
    _pdf_row(pdf, "Criado em",    data["created_at"][:16].replace("T", " ") + " UTC")
    pdf.ln(4)

    if meta.get("kg_cobre"):
        _pdf_section(pdf, "RESULTADO DO PROCESSAMENTO")
        _pdf_row(pdf, "Cobre recuperado",    f"{meta['kg_cobre']} kg", bold_value=True)
        _pdf_row(pdf, "Plástico recuperado", f"{meta.get('kg_plastico', '—')} kg")
        _pdf_row(pdf, "Resíduo",             f"{meta.get('kg_residuo', '—')} kg")
        taxa = round(float(meta["kg_cobre"]) / float(kg) * 100, 1) if kg and float(kg) > 0 else "—"
        _pdf_row(pdf, "Taxa de recuperação", f"{taxa}%")
        pdf.ln(4)

    _pdf_section(pdf, "HISTORICO DE ESTADOS")
    for ev in data["events"]:
        ts  = str(ev.get("ts", ""))[:16].replace("T", " ")
        msg = _s(str(ev.get("message", "")))[:90]
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(80, 90, 110)
        pdf.cell(35, 5, ts)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 5, msg, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    _pdf_section(pdf, "TAREFAS")
    for t in data["tasks"]:
        status_sym = "OK" if t["status"] == "done" else " - "
        done = t["status"] == "done"
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(20, 120, 20) if done else pdf.set_text_color(120, 120, 120)
        pdf.cell(10, 5, status_sym)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 5, _s(t["title"]), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, _s(f"Documento gerado automaticamente pelo AI-OS em {now_str}"), new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.output(str(fpath))
    return {"ok": True, "path": str(fpath), "filename": fname, "entity_id": entity_id}


def generate_fatura(entity_id: int) -> dict:
    """Gera PDF 'Fatura de Serviço'."""
    from fpdf import FPDF
    data = _get_batch(entity_id)
    if not data:
        return {"ok": False, "error": f"lote #{entity_id} não encontrado"}

    meta = data["meta"]
    if not meta.get("valor_fatura"):
        return {"ok": False, "error": "fatura não calculada — use /lote faturar primeiro"}

    client   = meta.get("client", "—")
    kg_cobre = float(meta.get("kg_cobre", 0))
    preco_kg = float(meta.get("preco_kg", 0))
    valor    = float(meta.get("valor_fatura", 0))
    now_str  = _nowstr()
    fname    = f"fatura_lote_{entity_id}.pdf"
    fpath    = DOCS_DIR / fname

    pdf = FPDF()
    pdf.add_page()
    _pdf_header(pdf, f"Fatura de Serviço — Lote #{entity_id}", f"Data: {now_str}")

    _pdf_section(pdf, "DADOS DO SERVIÇO")
    _pdf_row(pdf, "Nº do Lote",  f"#{entity_id}")
    _pdf_row(pdf, "Cliente",      client)
    _pdf_row(pdf, "Data",         now_str[:10])
    pdf.ln(4)

    _pdf_section(pdf, "DETALHE DE SERVICO")
    # Table header
    pdf.set_fill_color(220, 230, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(30, 60, 130)
    for h, w in [("Descricao", 80), ("Qtd (kg)", 30), ("Preco/kg (EUR)", 35), ("Total (EUR)", 35)]:
        pdf.cell(w, 7, h, border=1, fill=True)
    pdf.ln()
    # Row
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(80, 6, "Processamento cobre - servico reciclagem", border=1)
    pdf.cell(30, 6, f"{kg_cobre:.1f}", border=1, align="C")
    pdf.cell(35, 6, f"EUR{preco_kg:.4f}", border=1, align="C")
    pdf.cell(35, 6, f"EUR{valor:.2f}", border=1, align="R")
    pdf.ln(8)
    # Total
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(11, 15, 30)
    pdf.cell(145, 8, "TOTAL A PAGAR:", align="R")
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(0, 100, 50)
    pdf.cell(35, 8, f"EUR {valor:.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "IVA: isento (reciclagem de residuos - art.9 CIVA)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    _pdf_section(pdf, "INFORMACOES ADICIONAIS")
    _pdf_row(pdf, "Kg plastico",  f"{meta.get('kg_plastico', '-')} kg")
    _pdf_row(pdf, "Kg residuo",   f"{meta.get('kg_residuo', '-')} kg")
    _pdf_row(pdf, "Kg entrada",   f"{meta.get('kg', '-')} kg")

    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, _s(f"Documento gerado automaticamente pelo AI-OS em {now_str}"), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 4, "Este documento serve de comprovativo de servico prestado.", new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.output(str(fpath))
    return {"ok": True, "path": str(fpath), "filename": fname, "entity_id": entity_id,
            "valor": valor, "client": client}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: doc_engine.py guia|fatura <entity_id>", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    eid = int(sys.argv[2])

    # Load env
    env_db = Path.home() / ".env.db"
    if env_db.exists():
        for line in env_db.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    if cmd == "guia":
        print(json.dumps(generate_guia(eid)))
    elif cmd == "fatura":
        print(json.dumps(generate_fatura(eid)))
    else:
        print(json.dumps({"error": f"comando desconhecido: {cmd}"}))
