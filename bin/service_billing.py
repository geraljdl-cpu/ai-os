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
  python3 bin/service_billing.py get <token>
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


def validate_service_log(conn, token: str, approved: bool, note: str = "") -> dict:
    ts = get_by_token(conn, token)
    if not ts:
        raise ValueError(f"Token não encontrado: {token}")
    if ts["status"] not in ("submitted", "pending"):
        raise ValueError(f"Registo já processado: status={ts['status']}")

    ts_id   = ts["id"]
    action  = "approved" if approved else "rejected"
    payout_id = invoice_id = None

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.event_timesheets
            SET status=%s, validator_note=%s, validated_at=NOW()
            WHERE id=%s
        """, (action, note or None, ts_id))

    if approved:
        payout_id  = generate_payout(conn, ts_id, ts)
        invoice_id = generate_invoice_draft(conn, ts_id, ts)
        msg = f"Serviço validado: {ts.get('worker_id','?')} · {ts.get('log_date','?')} · {ts.get('days_equivalent','?')}d"
        level = "info"
    else:
        # Create incident for rejected service
        ex(conn, """
            INSERT INTO public.incidents (source, kind, severity, status, title, data)
            VALUES ('service_billing', 'service_rejected', 'warn', 'open', %s, %s::jsonb)
        """, (f"Serviço rejeitado: {ts.get('worker_id','?')} · {ts.get('log_date','?')}",
              json.dumps({"ts_id": ts_id, "note": note})))
        ex(conn, """
            INSERT INTO public.agent_suggestions (kind, title, details, score)
            VALUES ('alert', %s, %s, 8)
        """, (f"Serviço rejeitado: {ts.get('worker_id','?')} · {ts.get('log_date','?')}",
              note or "Cliente rejeitou o registo de serviço"))
        msg = f"Serviço rejeitado: {ts.get('worker_id','?')} · note: {note}"
        level = "warn"

    ex(conn, """
        INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
        VALUES (NOW(), %s, 'service_billing', 'service_log_validated', %s, %s, %s::jsonb)
    """, (level, ts_id, msg,
          json.dumps({"ts_id": ts_id, "action": action, "note": note,
                      "payout_id": payout_id, "invoice_id": invoice_id})))
    conn.commit()

    return {"ok": True, "ts_id": ts_id, "action": action,
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


def generate_invoice_draft(conn, ts_id: int, ts: dict) -> int:
    """Create twin_invoice draft for the service log."""
    person_name = ts.get("person_name") or ts.get("worker_id", "")
    log_date    = str(ts.get("log_date") or ts.get("start_time", "")[:10])
    days        = ts.get("days_equivalent") or 1
    total       = float(ts.get("invoice_total") or 0)

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
                          "net": float(ts.get("invoice_net") or 0),
                          "vat": float(ts.get("invoice_vat") or 0),
                          "service_type": "mao_de_obra"})))
        invoice_id = cur.fetchone()["id"]

    return invoice_id


def list_logs(conn, status: str | None = None, limit: int = 20) -> list:
    where = "WHERE ts.validation_token IS NOT NULL"
    params: list = []
    if status:
        where += " AND ts.status = %s"
        params.append(status)
    rows = q(conn, f"""
        SELECT ts.id, ts.worker_id, ts.log_date, ts.hours, ts.days_equivalent,
               ts.car_used, ts.location, ts.status, ts.worker_pay, ts.invoice_total,
               ts.validation_token, ts.created_at, ts.validated_at,
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
    approved = q1(conn, """
        SELECT COUNT(*) AS n, COALESCE(SUM(invoice_total),0) AS total
        FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status='approved'
          AND validated_at >= %s
    """, (month_start,))
    payout_total = q1(conn, """
        SELECT COALESCE(SUM(worker_pay),0) AS total
        FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status='approved'
          AND validated_at >= %s
    """, (month_start,)).get("total", 0)
    rejected = q1(conn, """
        SELECT COUNT(*) AS n FROM public.event_timesheets
        WHERE validation_token IS NOT NULL AND status='rejected'
    """).get("n", 0)

    return {
        "pending_validation": int(pending),
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

    p_get = sub.add_parser("get")
    p_get.add_argument("token")

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=20)

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
            result = validate_service_log(conn, args.token, args.approve, args.note)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "get":
            result = get_by_token(conn, args.token)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "list":
            result = list_logs(conn, args.status, args.limit)
            print(json.dumps(result, ensure_ascii=False, default=str))

        elif args.cmd == "stats":
            result = get_stats(conn)
            print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        conn.close()
