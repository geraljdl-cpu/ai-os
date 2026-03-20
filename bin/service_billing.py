#!/usr/bin/env python3
"""
service_billing.py — RH Service Billing Engine

Regras:
  ≤12h → 1 dia | >12h → 1.5 dias | >16h → 2 dias
  Worker: 50€/dia + 10€/dia carro
  Cliente: 100€/dia + 23% IVA

Usage:
  python3 bin/service_billing.py submit <person_id> <YYYY-MM-DD> <hours> <location> [--car]
  python3 bin/service_billing.py validate <token> --approve|--reject [--note "..."]
                                           [--adjusted-days 1.5] [--extras '[...]']
                                           [--expense-decisions '{...}'] [--ip "..."]
  python3 bin/service_billing.py get <token>
  python3 bin/service_billing.py get_with_expenses <token>
  python3 bin/service_billing.py add_expense <ts_id> --worker-id X --worker-name X --phone-mbway X --amount X
  python3 bin/service_billing.py review_expense <token> <expense_id> --approve|--reject [--reason "..."] [--ip "..."]
  python3 bin/service_billing.py reimburse_expense <token> <expense_id>
  python3 bin/service_billing.py list [--status submitted|approved|rejected] [--limit N]
  python3 bin/service_billing.py stats
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, datetime as dt, json, os, sys, uuid

import psycopg2
from psycopg2.extras import RealDictCursor

DSN = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")

UI_BASE = os.environ.get("UI_BASE", "http://localhost:3000")


def db():
    return psycopg2.connect(DSN, cursor_factory=RealDictCursor)


def q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q1(conn, sql, params=()):
    rows = q(conn, sql, params)
    return dict(rows[0]) if rows else {}


def ex(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ── Calculation ───────────────────────────────────────────────────────────────

def hours_to_days(hours: float, threshold_half: float = 12.0, threshold_full2: float = 16.0) -> float:
    """Convert hours worked to billing days."""
    if hours > threshold_full2:
        return 2.0
    if hours > threshold_half:
        return 1.5
    return 1.0


def get_default_rate(conn) -> dict:
    r = q1(conn, "SELECT * FROM public.service_rates WHERE active=TRUE AND name='standard' LIMIT 1")
    if not r:
        return {"rate_client_day": 100.00, "rate_worker_day": 50.00,
                "car_bonus_day": 10.00, "vat_rate": 23.00,
                "threshold_half": 12.0, "threshold_full2": 16.0}
    return r


def calc_all(hours: float, car_used: bool, rate: dict) -> dict:
    days     = hours_to_days(hours, float(rate["threshold_half"]), float(rate["threshold_full2"]))
    pay      = float(rate["rate_worker_day"]) * days + (float(rate["car_bonus_day"]) * days if car_used else 0)
    net      = float(rate["rate_client_day"]) * days
    vat      = net * float(rate["vat_rate"]) / 100
    total    = net + vat
    return {"days": days, "worker_pay": round(pay, 2),
            "invoice_net": round(net, 2), "invoice_vat": round(vat, 2), "invoice_total": round(total, 2)}


# ── Core functions ────────────────────────────────────────────────────────────

def get_person(conn, person_id: int) -> dict:
    return q1(conn, "SELECT id, name, nif FROM public.persons WHERE id=%s", (person_id,))


def submit_service_log(conn, person_id: int, log_date: str, hours: float,
                       location: str, car_used: bool = False, client_id=None) -> dict:
    person = get_person(conn, person_id)
    if not person:
        raise ValueError(f"Pessoa #{person_id} não encontrada")

    rate   = get_default_rate(conn)
    calc   = calc_all(hours, car_used, rate)
    token  = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.event_timesheets
              (worker_id, event_name, start_time, hours, status,
               log_date, days_equivalent, location, car_used, client_id,
               validation_token, worker_pay, invoice_net, invoice_vat, invoice_total,
               people_id)
            VALUES (%s, %s, NOW(), %s, 'submitted',
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s)
            RETURNING id
        """, (
            person["name"],
            f"Serviço {log_date}",
            hours,
            log_date, calc["days"], location, car_used, client_id,
            token, calc["worker_pay"], calc["invoice_net"], calc["invoice_vat"], calc["invoice_total"],
            person_id,
        ))
        ts_id = cur.fetchone()["id"]

    # Event log
    ex(conn, """
        INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
        VALUES (NOW(), 'info', 'service_billing', 'service_log_submitted', %s, %s, %s::jsonb)
    """, (ts_id, f"Serviço submetido: {person['name']} · {log_date} · {hours}h",
          json.dumps({"ts_id": ts_id, "person_id": person_id, "log_date": log_date,
                      "hours": hours, "days": calc["days"], "car_used": car_used,
                      "worker_pay": calc["worker_pay"], "invoice_total": calc["invoice_total"],
                      "token": token})))
    conn.commit()

    return {
        "ok": True, "ts_id": ts_id, "token": token,
        "person": person["name"], "log_date": log_date,
        "hours": hours, **calc,
        "validation_url": f"{UI_BASE}/validar/{token}",
    }


def get_by_token(conn, token: str) -> dict:
    row = q1(conn, """
        SELECT ts.*, p.name AS person_name, p.nif AS person_nif
        FROM public.event_timesheets ts
        LEFT JOIN public.persons p ON p.id = ts.people_id
        WHERE ts.validation_token = %s
    """, (token,))
    if not row:
        return {}
    # Convert types for JSON
    for k, v in row.items():
        if hasattr(v, 'isoformat'):
            row[k] = v.isoformat()
    return row


# ── Expenses ──────────────────────────────────────────────────────────────────

def list_expenses(conn, ts_id: int) -> list:
    rows = q(conn, """
        SELECT id, timesheet_id, worker_id, worker_name, worker_phone_mbway,
               client_id, receipt_image_url, receipt_name, receipt_nif_name,
               amount, expense_type, notes, status,
               approved_by, approved_at, rejected_reason, reimbursed_at,
               created_at, updated_at
        FROM public.timesheet_expenses
        WHERE timesheet_id = %s
        ORDER BY id
    """, (ts_id,))
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        result.append(d)
    return result


def get_by_token_with_expenses(conn, token: str) -> dict:
    row = get_by_token(conn, token)
    if not row:
        return {}
    row["expenses"] = list_expenses(conn, row["id"])
    return row


def add_expense(conn, ts_id: int, worker_id: str, worker_name: str,
                worker_phone_mbway: str, amount: float, expense_type: str,
                notes: str = "", receipt_name: str = "", receipt_nif_name: str = "",
                client_id: str = "") -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.timesheet_expenses
              (timesheet_id, worker_id, worker_name, worker_phone_mbway,
               amount, expense_type, notes, receipt_name, receipt_nif_name, client_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (ts_id, worker_id, worker_name, worker_phone_mbway,
              amount, expense_type, notes or None, receipt_name or None,
              receipt_nif_name or None, client_id or None))
        eid = cur.fetchone()["id"]
    conn.commit()
    return {"ok": True, "expense_id": eid}


def review_expense(conn, token: str, expense_id: int,
                   approved: bool, reason: str = "", remote_ip: str = "") -> dict:
    """Cliente aprova ou rejeita uma despesa individual (via token do timesheet)."""
    ts = get_by_token(conn, token)
    if not ts:
        raise ValueError("Token inválido")
    exp = q1(conn, """
        SELECT id, status FROM public.timesheet_expenses
        WHERE id = %s AND timesheet_id = %s
    """, (expense_id, ts["id"]))
    if not exp:
        raise ValueError(f"Despesa {expense_id} não encontrada para este token")
    new_status = "approved_client" if approved else "rejected_client"
    ex(conn, """
        UPDATE public.timesheet_expenses
        SET status=%s, approved_by=%s,
            approved_at = CASE WHEN %s THEN now() ELSE NULL END,
            rejected_reason = CASE WHEN NOT %s THEN %s ELSE NULL END,
            updated_at=now()
        WHERE id=%s
    """, (new_status, remote_ip or None, approved, approved, reason or None, expense_id))
    conn.commit()
    return {"ok": True, "expense_id": expense_id, "status": new_status}


def mark_reimbursed(conn, token: str, expense_id: int) -> dict:
    """Regista que o cliente efectuou o reembolso MB WAY."""
    ts = get_by_token(conn, token)
    if not ts:
        raise ValueError("Token inválido")
    ex(conn, """
        UPDATE public.timesheet_expenses
        SET status='reimbursed_mbway', reimbursed_at=now(), updated_at=now()
        WHERE id=%s AND timesheet_id=%s AND status='approved_client'
    """, (expense_id, ts["id"]))
    conn.commit()
    return {"ok": True, "expense_id": expense_id, "status": "reimbursed_mbway"}


def validate_service_log(conn, token: str, approved: bool, note: str = "",
                         adjusted_days: float = None, extras: list = None,
                         expense_decisions: dict = None, remote_ip: str = "") -> dict:
    ts = get_by_token(conn, token)
    if not ts:
        raise ValueError(f"Token não encontrado: {token}")
    if ts["status"] not in ("submitted", "pending", "approved_client", "adjusted_client"):
        raise ValueError(f"Registo já processado: status={ts['status']}")

    ts_id = ts["id"]
    extras = extras or []
    expense_decisions = expense_decisions or {}

    # Determine new status
    if approved:
        new_status = "adjusted_client" if (adjusted_days or extras) else "approved_client"
    else:
        new_status = "rejected_client"

    # Effective billing values
    rate     = get_default_rate(conn)
    eff_days = float(adjusted_days) if adjusted_days else float(ts.get("days_equivalent") or 1)
    net      = round(float(rate["rate_client_day"]) * eff_days, 2)
    vat      = round(net * float(rate["vat_rate"]) / 100, 2)
    extras_net   = round(sum(float(e.get("amount", 0)) for e in extras), 2)
    final_total  = round(net + vat + extras_net, 2)

    with conn.cursor() as cur:
        if adjusted_days or extras:
            cur.execute("""
                UPDATE public.event_timesheets
                SET status=%s, validator_note=%s, validated_at=NOW(),
                    adjusted_days=%s, adjusted_invoice_net=%s,
                    adjusted_invoice_vat=%s, adjusted_invoice_total=%s,
                    client_extras=%s::jsonb, approved_by=%s, updated_at=NOW()
                WHERE id=%s
            """, (new_status, note or None,
                  eff_days if adjusted_days else None,
                  net if adjusted_days else None,
                  vat if adjusted_days else None,
                  (net + vat) if adjusted_days else None,
                  json.dumps(extras), remote_ip or None, ts_id))
        else:
            cur.execute("""
                UPDATE public.event_timesheets
                SET status=%s, validator_note=%s, validated_at=NOW(),
                    approved_by=%s, updated_at=NOW()
                WHERE id=%s
            """, (new_status, note or None, remote_ip or None, ts_id))

    # Process expense decisions from client
    for eid_str, decision in expense_decisions.items():
        try:
            eid = int(eid_str)
            exp_approved = decision.get("status") == "approved_client"
            reason = decision.get("reason", "")
            exp_status = "approved_client" if exp_approved else "rejected_client"
            ex(conn, """
                UPDATE public.timesheet_expenses
                SET status=%s, approved_by=%s,
                    approved_at = CASE WHEN %s THEN now() ELSE NULL END,
                    rejected_reason = CASE WHEN NOT %s THEN %s ELSE NULL END,
                    updated_at=now()
                WHERE id=%s AND timesheet_id=%s
            """, (exp_status, remote_ip or None,
                  exp_approved, exp_approved, reason or None,
                  eid, ts_id))
        except (ValueError, KeyError, TypeError):
            pass

    payout_id = invoice_id = None
    if approved:
        ts_for_payout = dict(ts)
        if adjusted_days:
            ts_for_payout["worker_pay"] = round(float(rate["rate_worker_day"]) * eff_days, 2)
        payout_id  = generate_payout(conn, ts_id, ts_for_payout)
        ts_for_inv = dict(ts)
        if adjusted_days:
            ts_for_inv["days_equivalent"] = eff_days
            ts_for_inv["invoice_net"]     = net
            ts_for_inv["invoice_vat"]     = vat
            ts_for_inv["invoice_total"]   = net + vat
        invoice_id = generate_invoice_draft(conn, ts_id, ts_for_inv,
                                            extras=extras, final_total=final_total)
        msg   = f"Servico validado: {ts.get('worker_id','?')} · {ts.get('log_date','?')} · {eff_days}d"
        level = "info"
    else:
        ex(conn, """
            INSERT INTO public.incidents (source, kind, severity, status, title, data)
            VALUES ('service_billing', 'service_rejected', 'warn', 'open', %s, %s::jsonb)
        """, (f"Servico rejeitado: {ts.get('worker_id','?')} · {ts.get('log_date','?')}",
              json.dumps({"ts_id": ts_id, "note": note})))
        ex(conn, """
            INSERT INTO public.agent_suggestions (kind, title, details, score)
            VALUES ('alert', %s, %s, 8)
        """, (f"Servico rejeitado: {ts.get('worker_id','?')} · {ts.get('log_date','?')}",
              note or "Cliente rejeitou o registo de servico"))
        msg   = f"Servico rejeitado: {ts.get('worker_id','?')} · note: {note}"
        level = "warn"

    ex(conn, """
        INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
        VALUES (NOW(), %s, 'service_billing', 'service_log_validated', %s, %s, %s::jsonb)
    """, (level, ts_id, msg,
          json.dumps({"ts_id": ts_id, "action": new_status, "note": note,
                      "adjusted_days": adjusted_days, "extras_count": len(extras),
                      "expenses_decided": len(expense_decisions),
                      "payout_id": payout_id, "invoice_id": invoice_id})))
    conn.commit()

    # Auto-gerar mock invoice + email se validado
    if approved:
        try:
            import subprocess
            subprocess.Popen(
                [sys.executable, os.path.join(os.path.dirname(__file__), "invoice_mock.py"),
                 "draft_ts_and_send", str(ts_id)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    return {"ok": True, "ts_id": ts_id, "action": new_status,
            "effective_days": eff_days, "final_total": final_total,
            "payout_id": payout_id, "invoice_id": invoice_id}


def generate_payout(conn, ts_id: int, ts: dict) -> int:
    """Create or update finance_payout for the worker/week."""
    worker_id  = ts.get("worker_id", "")
    worker_pay = float(ts.get("worker_pay") or 0)
    log_date   = ts.get("log_date") or ts.get("start_time", "")[:10]

    # Week start (Monday)
    d = dt.date.fromisoformat(str(log_date)[:10])
    week_start = (d - dt.timedelta(days=d.weekday())).isoformat()

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.finance_payouts (worker_id, week_start, total_hours, amount, status)
            VALUES (%s, %s, %s, %s, 'pending')
            ON CONFLICT (worker_id, week_start)
            DO UPDATE SET
              total_hours = finance_payouts.total_hours + EXCLUDED.total_hours,
              amount      = finance_payouts.amount      + EXCLUDED.amount,
              updated_at  = NOW()
            RETURNING id
        """, (worker_id, week_start, float(ts.get("hours") or 0), worker_pay))
        payout_id = cur.fetchone()["id"]

    return payout_id


def generate_invoice_draft(conn, ts_id: int, ts: dict,
                           extras: list = None, final_total: float = None) -> int:
    """Create twin_invoice draft for the service log (supports multi-line extras)."""
    extras      = extras or []
    person_name = ts.get("person_name") or ts.get("worker_id", "")
    log_date    = str(ts.get("log_date") or ts.get("start_time", "")[:10])
    days        = ts.get("days_equivalent") or 1
    net         = float(ts.get("invoice_net") or 0)
    vat         = float(ts.get("invoice_vat") or 0)
    base_total  = round(net + vat, 2)

    lines = [{"type": "base", "description": f"Mão de obra ({float(days):.4g}d)",
              "net": net, "vat": vat, "total": base_total}]
    for e in extras:
        amt = round(float(e.get("amount", 0)), 2)
        lines.append({"type": "extra", "description": e.get("description", "Extra"),
                      "net": amt, "vat": 0.0, "total": amt})

    total = final_total if final_total is not None else base_total

    # Generate invoice number
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1 AS seq FROM public.twin_invoices")
        seq = cur.fetchone()["seq"]
        inv_num = f"SRV-{dt.date.today().year}-{seq:04d}"

        cur.execute("""
            INSERT INTO public.twin_invoices
              (number, status, amount, currency, client, due_date, metadata)
            VALUES (%s, 'draft', %s, 'EUR', %s, %s, %s::jsonb)
            RETURNING id
        """, (inv_num, total,
              person_name,
              (dt.date.today() + dt.timedelta(days=30)).isoformat(),
              json.dumps({"ts_id": ts_id, "log_date": log_date, "days": float(days or 0),
                          "car_used": ts.get("car_used", False),
                          "net": net, "vat": vat,
                          "service_type": "mao_de_obra",
                          "service_note": ts.get("notes") or "",
                          "lines": lines, "final_total": total})))
        invoice_id = cur.fetchone()["id"]

    return invoice_id


def add_manual_entry(conn, person_id: int, log_date: str, start_time: str, end_time: str,
                     event_name: str = "", notes: str = "", car_used: bool = False,
                     client_id=None) -> dict:
    """Registo manual de horas — source='manual', requires_review=True, status='draft'."""
    person = get_person(conn, person_id)
    if not person:
        raise ValueError(f"Pessoa #{person_id} nao encontrada")

    # Calcular horas
    fmt = "%H:%M"
    try:
        t_in  = dt.datetime.strptime(start_time, fmt)
        t_out = dt.datetime.strptime(end_time, fmt)
        hours = round((t_out - t_in).seconds / 3600, 2)
        if hours <= 0:
            raise ValueError("Hora saida deve ser posterior a hora entrada")
    except ValueError as e:
        if "posterior" in str(e):
            raise
        raise ValueError(f"Formato invalido (use HH:MM): {e}")

    rate  = get_default_rate(conn)
    calc  = calc_all(hours, car_used, rate)
    token = str(uuid.uuid4())
    ev    = event_name or f"Servico {log_date}"

    # Construir datetime completo para start/end
    d_base = dt.date.fromisoformat(log_date)
    start_dt = dt.datetime.combine(d_base, dt.datetime.strptime(start_time, fmt).time())
    end_dt   = dt.datetime.combine(d_base, dt.datetime.strptime(end_time,   fmt).time())

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.event_timesheets
              (worker_id, event_name, start_time, end_time, hours, status,
               log_date, days_equivalent, location, car_used, client_id,
               validation_token, worker_pay, invoice_net, invoice_vat, invoice_total,
               people_id, notes, source, requires_review)
            VALUES (%s, %s, %s, %s, %s, 'draft',
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, 'manual', TRUE)
            RETURNING id
        """, (
            person["name"], ev, start_dt, end_dt, hours,
            log_date, calc["days"], "Manual", car_used, client_id,
            token, calc["worker_pay"], calc["invoice_net"], calc["invoice_vat"], calc["invoice_total"],
            person_id, notes or None,
        ))
        ts_id = cur.fetchone()["id"]

    ex(conn, """
        INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
        VALUES (NOW(), 'info', 'service_billing', 'manual_entry_created', %s, %s, %s::jsonb)
    """, (ts_id, f"Registo manual: {person['name']} · {log_date} · {hours}h",
          json.dumps({"ts_id": ts_id, "person_id": person_id, "log_date": log_date,
                      "hours": hours, "source": "manual", "requires_review": True})))
    conn.commit()

    return {
        "ok": True, "ts_id": ts_id, "token": token,
        "person": person["name"], "log_date": log_date,
        "hours": hours, "source": "manual", "requires_review": True,
        **calc,
    }


def list_logs(conn, status: str | None = None, limit: int = 20) -> list:
    # Include draft (manual) entries + all others
    if status:
        where  = "WHERE ts.status = %s"
        params = [status]
    else:
        where  = "WHERE (ts.validation_token IS NOT NULL OR ts.source='manual')"
        params = []
    rows = q(conn, f"""
        SELECT ts.id, ts.worker_id, ts.log_date, ts.hours, ts.days_equivalent,
               ts.car_used, ts.location, ts.notes, ts.status, ts.worker_pay, ts.invoice_total,
               ts.validation_token, ts.created_at, ts.validated_at,
               ts.source, ts.requires_review,
               p.name AS person_name
        FROM public.event_timesheets ts
        LEFT JOIN public.persons p ON p.id = ts.people_id
        {where}
        ORDER BY ts.created_at DESC
        LIMIT %s
    """, params + [limit])
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        result.append(d)
    return result


def get_stats(conn) -> dict:
    today       = dt.date.today()
    month_start = today.replace(day=1).isoformat()

    pending = q1(conn, """
        SELECT COUNT(*) AS n FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status='submitted'
    """).get("n", 0)
    manual_review = q1(conn, """
        SELECT COUNT(*) AS n FROM public.event_timesheets
        WHERE source='manual' AND requires_review=TRUE AND status='draft'
    """).get("n", 0)
    approved = q1(conn, """
        SELECT COUNT(*) AS n, COALESCE(SUM(invoice_total),0) AS total
        FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status IN ('approved','validated','invoiced_mock')
          AND validated_at >= %s
    """, (month_start,))
    payout_total = q1(conn, """
        SELECT COALESCE(SUM(worker_pay),0) AS total
        FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status IN ('approved','validated','invoiced_mock')
          AND validated_at >= %s
    """, (month_start,)).get("total", 0)
    rejected = q1(conn, """
        SELECT COUNT(*) AS n FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status='rejected'
    """).get("n", 0)

    return {
        "pending_validation": int(pending),
        "manual_review":      int(manual_review),
        "approved_month":     int(approved.get("n", 0)),
        "invoiced_month":     float(approved.get("total", 0)),
        "payout_month":       float(payout_total),
        "rejected_total":     int(rejected),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub    = parser.add_subparsers(dest="cmd")

    p_sub = sub.add_parser("submit")
    p_sub.add_argument("person_id", type=int)
    p_sub.add_argument("log_date")
    p_sub.add_argument("hours", type=float)
    p_sub.add_argument("location")
    p_sub.add_argument("--car", action="store_true")
    p_sub.add_argument("--client-id", type=int, default=None)

    p_val = sub.add_parser("validate")
    p_val.add_argument("token")
    g = p_val.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject",  action="store_true")
    p_val.add_argument("--note", default="")
    p_val.add_argument("--adjusted-days", type=float, default=None)
    p_val.add_argument("--extras", default="[]",
                       help='JSON array: [{"description":"X","amount":10}]')
    p_val.add_argument("--expense-decisions", default="{}",
                       help='JSON dict: {"1":{"status":"approved_client"},...}')
    p_val.add_argument("--ip", default="")

    p_get = sub.add_parser("get")
    p_get.add_argument("token")

    p_gwe = sub.add_parser("get_with_expenses")
    p_gwe.add_argument("token")

    p_ae = sub.add_parser("add_expense")
    p_ae.add_argument("ts_id", type=int)
    p_ae.add_argument("--worker-id",    required=True)
    p_ae.add_argument("--worker-name",  required=True)
    p_ae.add_argument("--phone-mbway",  required=True)
    p_ae.add_argument("--amount",       type=float, required=True)
    p_ae.add_argument("--type",         default="other", dest="expense_type")
    p_ae.add_argument("--notes",        default="")
    p_ae.add_argument("--receipt-name", default="")
    p_ae.add_argument("--nif-name",     default="")
    p_ae.add_argument("--client-id",    default="")

    p_re = sub.add_parser("review_expense")
    p_re.add_argument("token")
    p_re.add_argument("expense_id", type=int)
    g2 = p_re.add_mutually_exclusive_group(required=True)
    g2.add_argument("--approve", action="store_true")
    g2.add_argument("--reject",  action="store_true")
    p_re.add_argument("--reason", default="")
    p_re.add_argument("--ip", default="")

    p_rmb = sub.add_parser("reimburse_expense")
    p_rmb.add_argument("token")
    p_rmb.add_argument("expense_id", type=int)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=20)

    p_man = sub.add_parser("manual")
    p_man.add_argument("person_id", type=int)
    p_man.add_argument("log_date")
    p_man.add_argument("start_time")   # HH:MM
    p_man.add_argument("end_time")     # HH:MM
    p_man.add_argument("--event", default="")
    p_man.add_argument("--notes", default="")
    p_man.add_argument("--car", action="store_true")
    p_man.add_argument("--client-id", type=int, default=None)

    p_promote = sub.add_parser("promote")  # draft → submitted
    p_promote.add_argument("ts_id", type=int)

    sub.add_parser("stats")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    conn = db()
    try:
        if args.cmd == "submit":
            result = submit_service_log(conn, args.person_id, args.log_date,
                                        args.hours, args.location, args.car,
                                        getattr(args, "client_id", None))
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "validate":
            extras           = json.loads(args.extras)
            expense_decisions = json.loads(args.expense_decisions)
            result = validate_service_log(
                conn, args.token, args.approve, args.note,
                adjusted_days=args.adjusted_days,
                extras=extras, expense_decisions=expense_decisions,
                remote_ip=args.ip,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "get":
            result = get_by_token(conn, args.token)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "get_with_expenses":
            result = get_by_token_with_expenses(conn, args.token)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "add_expense":
            result = add_expense(
                conn, args.ts_id,
                worker_id=args.worker_id, worker_name=args.worker_name,
                worker_phone_mbway=args.phone_mbway, amount=args.amount,
                expense_type=args.expense_type, notes=args.notes,
                receipt_name=args.receipt_name, receipt_nif_name=args.nif_name,
                client_id=args.client_id,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "review_expense":
            result = review_expense(
                conn, args.token, args.expense_id, args.approve,
                reason=args.reason, remote_ip=args.ip,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "reimburse_expense":
            result = mark_reimbursed(conn, args.token, args.expense_id)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "list":
            result = list_logs(conn, args.status, args.limit)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "manual":
            result = add_manual_entry(
                conn, args.person_id, args.log_date,
                args.start_time, args.end_time,
                event_name=args.event, notes=args.notes,
                car_used=args.car, client_id=getattr(args, "client_id", None),
            )
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "promote":
            # Promover draft → submitted (after human review)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.event_timesheets
                    SET status='submitted', requires_review=FALSE, updated_at=NOW()
                    WHERE id=%s AND status='draft'
                    RETURNING id, worker_id, log_date
                """, (args.ts_id,))
                row = cur.fetchone()
            conn.commit()
            if row:
                print(json.dumps({"ok": True, "ts_id": row["id"],
                                  "worker_id": row["worker_id"], "log_date": str(row["log_date"])},
                                 ensure_ascii=False))
            else:
                print(json.dumps({"ok": False, "error": f"Timesheet {args.ts_id} nao encontrado ou nao em draft"}))

        elif args.cmd == "stats":
            result = get_stats(conn)
            print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        conn.close()
