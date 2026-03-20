#!/usr/bin/env python3
"""
invoice_mock.py — gerador de faturas simuladas (modo sandbox).

Fluxo:
  invoice_mode = "mock" → gera PDF com watermark "DOCUMENTO SIMULADO — SEM VALOR LEGAL"
  NÃO usa Toconline, NÃO regista como fatura oficial.
  Número: MOCK-FT-YYYY-XXX

Uso:
  python3 bin/invoice_mock.py generate <invoice_id>
  python3 bin/invoice_mock.py test
  python3 bin/invoice_mock.py list
  python3 bin/invoice_mock.py send_email <invoice_id>
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys
from datetime import datetime, date

import sqlalchemy as sa

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
MOCK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "runtime", "invoices", "mock")
os.makedirs(MOCK_DIR, exist_ok=True)


def _engine():
    return sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)


def _get_invoice_mode() -> str:
    """Lê invoice_mode do system_config."""
    engine = _engine()
    with engine.connect() as c:
        v = c.execute(sa.text("SELECT value FROM public.system_config WHERE key='invoice_mode'")).scalar()
    return (v or "mock").strip().lower()


def _next_mock_number(c) -> str:
    """Gera próximo número MOCK-FT-YYYY-XXX."""
    year = datetime.now().year
    prefix = f"MOCK-FT-{year}-"
    row = c.execute(sa.text(
        "SELECT number FROM public.twin_invoices WHERE number LIKE :p ORDER BY number DESC LIMIT 1"
    ), {"p": f"{prefix}%"}).fetchone()
    if row:
        last = int(row[0].split("-")[-1])
        seq = last + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def generate_mock_pdf(invoice_data: dict) -> str:
    """
    Gera PDF simulado com watermark.
    Retorna path do ficheiro gerado.
    """
    from fpdf import FPDF

    number = invoice_data.get("number", "MOCK-FT-????-000")
    client = invoice_data.get("client", "Cliente Não Definido")
    description = invoice_data.get("description", "Serviços prestados")
    net = float(invoice_data.get("net", 0) or 0)
    vat_rate = float(invoice_data.get("vat_rate", 23) or 23)
    vat = round(net * vat_rate / 100, 2)
    total = round(net + vat, 2)
    issue_date = invoice_data.get("issue_date", date.today().isoformat())
    due_date = invoice_data.get("due_date", "")
    items = invoice_data.get("items", [])

    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=False)

    # --- Watermark ---
    pdf.set_font("Helvetica", "B", 40)
    pdf.set_text_color(220, 60, 60)
    with pdf.rotation(45, x=105, y=148):
        pdf.set_xy(15, 120)
        pdf.cell(180, 20, "DOCUMENTO SIMULADO", align="C")
        pdf.set_xy(15, 140)
        pdf.set_font("Helvetica", "B", 22)
        pdf.cell(180, 12, "SEM VALOR LEGAL", align="C")

    # --- Header ---
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(10, 10)
    pdf.cell(120, 10, "AI-OS - Fatura Simulada",
             new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(140, 10)
    pdf.cell(60, 5, f"N.: {number}", align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(140, 15)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(60, 5, f"Data: {issue_date}", align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if due_date:
        pdf.set_xy(140, 20)
        pdf.cell(60, 5, f"Vencimento: {due_date}", align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, 28, 200, 28)

    # --- Client ---
    pdf.set_xy(10, 32)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(40, 5, "CLIENTE:", new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, client[:60], new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Items table ---
    pdf.set_xy(10, 45)
    pdf.set_fill_color(40, 60, 100)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(110, 7, "Descricao", fill=True, border=0,
             new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(25, 7, "Qtd", fill=True, border=0, align="C",
             new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(25, 7, "Preco Unit.", fill=True, border=0, align="R",
             new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(25, 7, "Total", fill=True, border=0, align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)
    y = 52
    if items:
        for item in items:
            desc = str(item.get("desc", description))[:60]
            qty = item.get("qty", 1)
            unit = float(item.get("unit_price", net) or 0)
            subtotal = round(float(qty) * unit, 2)
            pdf.set_xy(10, y)
            pdf.cell(110, 6, desc, new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(25, 6, str(qty), align="C", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(25, 6, f"{unit:.2f}EUR", align="R", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(25, 6, f"{subtotal:.2f}EUR", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            y += 6
    else:
        pdf.set_xy(10, y)
        pdf.cell(110, 6, description[:60], new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(25, 6, "1", align="C", new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(25, 6, f"{net:.2f}EUR", align="R", new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(25, 6, f"{net:.2f}EUR", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y += 6

    pdf.line(10, y + 2, 200, y + 2)

    # --- Totals ---
    y += 8
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(140, y)
    pdf.cell(35, 5, "Subtotal:", new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(20, 5, f"{net:.2f}EUR", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(140, y + 6)
    pdf.cell(35, 5, f"IVA ({vat_rate:.0f}%):", new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(20, 5, f"{vat:.2f}EUR", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_xy(140, y + 12)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 6, "TOTAL:", new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.cell(20, 6, f"{total:.2f}EUR", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Footer ---
    pdf.set_y(-25)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, "SIMULACAO - Sem valor legal ou fiscal.",
             align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 5, f"Gerado por AI-OS em {datetime.now().strftime('%Y-%m-%d %H:%M')} | invoice_mode=mock",
             align="C")

    # Save
    fname = f"{number}.pdf"
    path = os.path.join(MOCK_DIR, fname)
    pdf.output(path)
    return path


def create_mock_invoice(invoice_id: int = None, data: dict = None) -> dict:
    """
    Cria ou actualiza fatura mock a partir de twin_invoices.
    Se invoice_id fornecido, usa dados da DB.
    Se data fornecido, usa esses dados directamente.
    """
    engine = _engine()
    with engine.connect() as c:
        if invoice_id:
            row = c.execute(sa.text(
                "SELECT id, number, client, amount, metadata, due_date, created_at "
                "FROM public.twin_invoices WHERE id=:id"
            ), {"id": invoice_id}).fetchone()
            if not row:
                return {"ok": False, "error": f"Invoice {invoice_id} não encontrada"}
            inv_data = {
                "number": row.number or _next_mock_number(c),
                "client": row.client or "Sem cliente",
                "description": (row.metadata or {}).get("description", "Serviços AI-OS"),
                "net": float(row.amount or 0),
                "vat_rate": 23,
                "issue_date": row.created_at.date().isoformat() if row.created_at else date.today().isoformat(),
                "due_date": row.due_date.isoformat() if row.due_date else "",
                "items": (row.metadata or {}).get("items", []),
            }
        elif data:
            inv_data = data
            if "number" not in inv_data:
                inv_data["number"] = _next_mock_number(c)
        else:
            return {"ok": False, "error": "Fornecer invoice_id ou data"}

        # Generate PDF
        pdf_path = generate_mock_pdf(inv_data)

        # Update DB if we have an id
        if invoice_id:
            meta = dict((row.metadata or {})) if invoice_id else {}
            meta["mock_pdf"] = pdf_path
            meta["mock_generated_at"] = datetime.now().isoformat()
            c.execute(sa.text(
                "UPDATE public.twin_invoices SET pdf_path=:p, metadata=:m WHERE id=:id"
            ), {"p": pdf_path, "m": json.dumps(meta), "id": invoice_id})
            c.commit()

    return {
        "ok": True,
        "number": inv_data["number"],
        "pdf_path": pdf_path,
        "client": inv_data["client"],
        "total": round(float(inv_data.get("net", 0)) * 1.23, 2),
    }


def generate_mock_from_scratch(
    client: str,
    description: str,
    net: float,
    vat_rate: float = 23.0,
    items: list = None,
    email_to: str = None,
    extra_meta: dict = None,
    draft: bool = False,
) -> dict:
    """
    Cria fatura mock do zero (sem registro prévio em twin_invoices).
    Gera número sequencial MOCK-FT-YYYY-XXX.
    """
    engine = _engine()
    with engine.connect() as c:
        number = _next_mock_number(c)
        vat = round(net * vat_rate / 100, 2)
        total = round(net + vat, 2)
        issue_date = date.today().isoformat()

        inv_data = {
            "number": number,
            "client": client,
            "description": description,
            "net": net,
            "vat_rate": vat_rate,
            "issue_date": issue_date,
            "items": items or [],
        }
        pdf_path = generate_mock_pdf(inv_data)

        # Registar em twin_invoices
        inv_status = 'mock_draft' if draft else 'mock'
        meta_obj = {
            "description": description,
            "net": net,
            "vat": vat,
            "vat_rate": vat_rate,
            "invoice_mode": "mock",
            "items": items or [],
        }
        if extra_meta:
            meta_obj.update(extra_meta)
        row = c.execute(sa.text("""
            INSERT INTO public.twin_invoices
              (number, client, amount, status, pdf_path, metadata, created_at, updated_at)
            VALUES (:num, :cli, :amt, :st, :pdf,
                    CAST(:meta AS jsonb), NOW(), NOW())
            RETURNING id
        """), {
            "num": number,
            "cli": client,
            "amt": total,
            "st": inv_status,
            "pdf": pdf_path,
            "meta": json.dumps(meta_obj),
        }).fetchone()
        c.commit()
        inv_id = row[0] if row else None

    result = {
        "ok": True,
        "id": inv_id,
        "number": number,
        "client": client,
        "net": net,
        "vat": vat,
        "total": total,
        "pdf_path": pdf_path,
        "issue_date": issue_date,
    }

    if email_to:
        result["email"] = send_mock_email(email_to, result)

    return result


def send_mock_email(to: str, invoice: dict) -> dict:
    """Envia email de simulação com o PDF em anexo."""
    import smtplib, os
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    # Load SMTP config from /etc/aios.env
    env = {}
    try:
        for line in open("/etc/aios.env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass

    smtp_host = env.get("SMTP_HOST", "smtp.zoho.eu")
    smtp_port = int(env.get("SMTP_PORT", "587"))
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    smtp_from = env.get("SMTP_FROM", smtp_user)

    number = invoice["number"]
    client = invoice["client"]
    total  = float(invoice.get("total", 0))
    pdf_path = invoice.get("pdf_path", "")

    try:
        from email.utils import formataddr
        from email.header import Header
        # Use plain user for relay, display name encoded separately
        bare_from = smtp_user
        display   = smtp_from  # may contain unicode
        try:
            display.encode("ascii")
            from_hdr = formataddr((display, bare_from))
        except UnicodeEncodeError:
            from_hdr = bare_from  # fallback to plain email

        msg = MIMEMultipart("mixed")
        msg["From"]    = from_hdr
        msg["To"]      = to
        msg["Subject"] = f"[SIMULACAO] Faturacao - {number} - {client}"

        body_html = f"""<div style="font-family:sans-serif;max-width:600px;margin:0 auto">
  <div style="background:#1e3a5f;color:white;padding:20px;text-align:center">
    <h2 style="margin:0">Simulacao de Faturacao</h2>
  </div>
  <div style="padding:20px;background:#f9f9f9">
    <div style="background:#fff3cd;border:2px solid #ffc107;padding:12px;border-radius:6px;margin-bottom:16px">
      <strong>DOCUMENTO DE TESTE - SEM VALOR LEGAL</strong>
    </div>
    <p>Segue em anexo o documento simulado <strong>{number}</strong> para <strong>{client}</strong>.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0">
      <tr style="background:#f0f0f0"><td style="padding:8px;font-weight:bold">N. Documento</td><td style="padding:8px">{number}</td></tr>
      <tr><td style="padding:8px;font-weight:bold">Cliente</td><td style="padding:8px">{client}</td></tr>
      <tr style="background:#f0f0f0"><td style="padding:8px;font-weight:bold">Total (c/ IVA)</td><td style="padding:8px"><strong>{total:.2f} EUR</strong></td></tr>
    </table>
    <p style="color:#888;font-size:12px">Enviado automaticamente pelo AI-OS | invoice_mode=mock</p>
  </div>
</div>"""

        msg.attach(MIMEText(body_html, "html", "utf-8"))

        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(pdf_path))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(pdf_path)}"'
            msg.attach(part)

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_from, [to], msg.as_string())

        return {"ok": True, "to": to}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def draft_ts_and_send(ts_id: int) -> dict:
    """Cria draft mock + envia email ao cliente + actualiza ts para invoiced_mock."""
    # Primeiro criar/obter draft
    result = draft_from_timesheet(ts_id)
    if not result.get("ok"):
        return result

    # Obter email do cliente a partir do timesheet
    engine = _engine()
    with engine.connect() as c:
        row = c.execute(sa.text("""
            SELECT wc.default_client_email, p.email AS person_email
            FROM public.event_timesheets ts
            LEFT JOIN public.worker_contacts wc ON wc.worker_name = ts.worker_id
            LEFT JOIN public.persons p ON p.id = ts.people_id
            WHERE ts.id = :id
        """), {"id": ts_id}).mappings().first()

    inv_id   = result["id"]
    email_to = None
    if row:
        email_to = row.get("default_client_email") or row.get("person_email")

    if not email_to:
        # Sem email de cliente configurado — apenas marcar como mock_draft
        return {**result, "email": {"ok": False, "error": "Sem email de cliente configurado"}}

    # Enviar
    email_result = send_draft(inv_id, email_to)

    if email_result.get("ok"):
        # Actualizar timesheet para invoiced_mock
        with engine.begin() as c:
            c.execute(sa.text(
                "UPDATE public.event_timesheets SET status='invoiced_mock', updated_at=NOW() WHERE id=:id"
            ), {"id": ts_id})

    return {**result, "email": email_result, "email_to": email_to}


def draft_from_timesheet(ts_id: int) -> dict:
    """Cria rascunho mock a partir de um timesheet aprovado (sem enviar email)."""
    engine = _engine()
    with engine.connect() as c:
        # Verificar se já existe draft para este timesheet
        existing = c.execute(sa.text(
            "SELECT id, number FROM public.twin_invoices "
            "WHERE metadata->>'timesheet_id' = :tsid AND status IN ('mock_draft','mock') LIMIT 1"
        ), {"tsid": str(ts_id)}).fetchone()
        if existing:
            return {"ok": True, "id": existing[0], "number": existing[1], "already_exists": True}

        # Carregar dados do timesheet
        row = c.execute(sa.text("""
            SELECT ts.id, ts.worker_id, ts.event_name, ts.notes, ts.log_date,
                   ts.hours, ts.days_equivalent, ts.worker_pay,
                   ts.invoice_net, ts.invoice_vat, ts.invoice_total,
                   ts.hourly_rate, p.name AS person_name, p.email AS person_email
            FROM public.event_timesheets ts
            LEFT JOIN public.persons p ON p.id = ts.people_id
            WHERE ts.id = :id
        """), {"id": ts_id}).mappings().first()

    if not row:
        return {"ok": False, "error": f"Timesheet {ts_id} nao encontrado"}

    worker  = row["person_name"] or row["worker_id"] or "Colaborador"
    notes   = (row["notes"] or row["event_name"] or "").strip()
    log_date = row["log_date"]
    date_s  = str(log_date or "")[:10]
    # Format date as DD/MM/YYYY for the invoice description
    try:
        import datetime as _dt
        date_fmt = _dt.date.fromisoformat(date_s).strftime("%d/%m/%Y")
    except Exception:
        date_fmt = date_s
    hours   = float(row["hours"] or 0)
    net     = float(row["invoice_net"] or row["worker_pay"] or 0)
    vat     = float(row["invoice_vat"] or 0)
    total   = float(row["invoice_total"] or (net + vat))
    if net == 0 and total > 0:
        net = round(total / 1.23, 2)
        vat = round(total - net, 2)

    # Description built from worker's note: "Prestacao de servico - Altice Arena - 19/03/2026"
    note_part = f" - {notes.title()}" if notes else ""
    description = f"Prestacao de servico{note_part} - {date_fmt}"
    items = [{"desc": description, "qty": 1, "unit_price": net}]

    result = generate_mock_from_scratch(
        client=worker,
        description=description,
        net=net,
        vat_rate=23.0 if vat == 0 else round(vat / net * 100, 1) if net > 0 else 23.0,
        items=items,
        email_to=None,  # draft — sem email
        extra_meta={"timesheet_id": ts_id, "person_email": row.get("person_email") or ""},
        draft=True,
    )
    return result


def send_draft(invoice_id: int, email_to: str) -> dict:
    """Envia rascunho mock e actualiza status para 'mock'."""
    engine = _engine()
    with engine.connect() as c:
        row = c.execute(sa.text(
            "SELECT id, number, client, amount, pdf_path, metadata FROM public.twin_invoices WHERE id=:id"
        ), {"id": invoice_id}).mappings().first()
    if not row:
        return {"ok": False, "error": f"Invoice {invoice_id} nao encontrada"}

    meta = dict(row["metadata"] or {})
    invoice = {
        "number": row["number"],
        "client": row["client"],
        "total":  float(row["amount"] or 0),
        "pdf_path": row["pdf_path"] or "",
    }
    result = send_mock_email(email_to, invoice)
    if result.get("ok"):
        with engine.begin() as c:
            c.execute(sa.text(
                "UPDATE public.twin_invoices SET status='mock', updated_at=NOW() WHERE id=:id"
            ), {"id": invoice_id})
    return {**result, "number": invoice["number"], "client": invoice["client"]}


def list_mock_invoices(limit: int = 20) -> list:
    """Lista faturas mock da DB."""
    engine = _engine()
    with engine.connect() as c:
        rows = c.execute(sa.text(
            "SELECT id, number, client, amount, status, pdf_path, created_at "
            "FROM public.twin_invoices WHERE status='mock' OR number LIKE 'MOCK-%' "
            "ORDER BY id DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    return [dict(r._mapping) for r in rows]


# CLI
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "test":
        # Gerar fatura de teste
        result = generate_mock_from_scratch(
            client="Cliente Teste, Lda",
            description="Consultoria AI-OS - Marco 2026",
            net=1000.00,
            items=[
                {"desc": "Horas de consultoria (10h x 80 EUR)", "qty": 10, "unit_price": 80},
                {"desc": "Deslocações", "qty": 1, "unit_price": 200},
            ],
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "generate":
        if len(sys.argv) < 3:
            print("Uso: invoice_mock.py generate <invoice_id>")
            sys.exit(1)
        result = create_mock_invoice(invoice_id=int(sys.argv[2]))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "list":
        rows = list_mock_invoices()
        for r in rows:
            print(f"  {r['number']:25} | {str(r['client'])[:30]:30} | {float(r['amount'] or 0):8.2f} EUR | {r['status']}")

    elif cmd == "send_email":
        if len(sys.argv) < 4:
            print("Uso: invoice_mock.py send_email <invoice_id> <email>")
            sys.exit(1)
        inv_id = int(sys.argv[2])
        email_to = sys.argv[3]
        result = create_mock_invoice(invoice_id=inv_id)
        if result["ok"]:
            r2 = send_mock_email(email_to, result)
            print(json.dumps({**result, "email": r2}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result))

    elif cmd == "generate_api":
        # Called by server.js: invoice_mock.py generate_api <json_args>
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = generate_mock_from_scratch(
            client=args.get("client", ""),
            description=args.get("description", ""),
            net=float(args.get("net", 0)),
            vat_rate=float(args.get("vat_rate", 23)),
            items=args.get("items") or [],
            email_to=args.get("email_to") or None,
        )
        print(json.dumps(result, ensure_ascii=False))

    elif cmd == "list_api":
        rows = list_mock_invoices()
        print(json.dumps({"ok": True, "invoices": rows}, ensure_ascii=False, default=str))

    elif cmd == "get_pdf":
        inv_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        result = create_mock_invoice(invoice_id=inv_id)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif cmd == "draft_ts":
        # Called by server.js after timesheet approval
        ts_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        result = draft_from_timesheet(ts_id)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif cmd == "draft_ts_and_send":
        # Called by service_billing after client validation
        ts_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        result = draft_ts_and_send(ts_id)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif cmd == "send_draft_api":
        # Called by server.js: send_draft_api <invoice_id> <email>
        inv_id   = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        email_to = sys.argv[3] if len(sys.argv) > 3 else ""
        result   = send_draft(inv_id, email_to)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif cmd == "drafts_api":
        # List mock_draft invoices (for the UI)
        engine = _engine()
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT id, number, client, amount, status, pdf_path, metadata, created_at "
                "FROM public.twin_invoices WHERE status='mock_draft' ORDER BY id DESC LIMIT 50"
            )).fetchall()
        result = [dict(r._mapping) for r in rows]
        print(json.dumps({"ok": True, "drafts": result}, ensure_ascii=False, default=str))

    else:
        print(__doc__)
