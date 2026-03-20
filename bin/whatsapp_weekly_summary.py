#!/usr/bin/env python3
"""
whatsapp_weekly_summary.py — Resumo semanal WhatsApp para colaboradores
Corre às 2ª-feira 08:00, envia resumo da semana anterior (seg-dom)

Uso:
  python3 bin/whatsapp_weekly_summary.py           # semana anterior
  python3 bin/whatsapp_weekly_summary.py --dry-run # só imprime, não envia
  python3 bin/whatsapp_weekly_summary.py --week 2026-03-10  # semana específica (seg)
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, datetime as dt, json, logging, os

import psycopg2
from psycopg2.extras import RealDictCursor

DSN           = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
AIOS_ROOT     = os.path.dirname(_bin_dir)
_log_dir      = os.path.join(AIOS_ROOT, "runtime", "whatsapp")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(_log_dir, "weekly_summary.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def last_week_range() -> tuple[dt.date, dt.date]:
    """Returns (monday, sunday) of the previous week."""
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    last_monday = this_monday - dt.timedelta(weeks=1)
    last_sunday = last_monday + dt.timedelta(days=6)
    return last_monday, last_sunday


def get_week_timesheets(conn, week_start: dt.date, week_end: dt.date) -> list:
    """Get all submitted/approved timesheets for the week, per worker."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT et.worker_id, et.worker_phone, et.log_date, et.hours,
                   et.days_equivalent, et.worker_pay, et.car_used,
                   et.notes, et.location, et.status,
                   wc.worker_name, wc.whatsapp_phone
            FROM public.event_timesheets et
            LEFT JOIN public.worker_contacts wc ON wc.whatsapp_phone = et.worker_phone
            WHERE et.log_date BETWEEN %s AND %s
              AND et.validation_token IS NOT NULL
              AND et.status IN ('submitted','approved','paid')
            ORDER BY et.worker_phone, et.log_date
        """, (week_start, week_end))
        return [dict(r) for r in cur.fetchall()]


def get_active_workers(conn) -> list:
    """All active workers with their phones."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT worker_name, whatsapp_phone
            FROM public.worker_contacts
            WHERE active = TRUE AND whatsapp_phone NOT LIKE '+351000%'
        """)
        return [dict(r) for r in cur.fetchall()]


def build_summary(worker_name: str, entries: list,
                  week_start: dt.date, week_end: dt.date) -> str:
    total_days = sum(float(e.get("days_equivalent") or 0) for e in entries)
    total_pay  = sum(float(e.get("worker_pay") or 0) for e in entries)
    pending    = [e for e in entries if e["status"] == "submitted"]

    week_str = f"{week_start.strftime('%d/%m')} – {week_end.strftime('%d/%m/%Y')}"
    lines = [f"📊 *Resumo semana {week_str}*\n"]

    for e in entries:
        d = e["log_date"]
        date_str = d.strftime("%d/%m") if hasattr(d, "strftime") else str(d)[:5]
        days = float(e.get("days_equivalent") or 0)
        pay  = float(e.get("worker_pay") or 0)
        note = e.get("notes") or "Serviço"
        car  = " 🚗" if e.get("car_used") else ""
        status = "✅" if e["status"] in ("approved","paid") else "⏳"
        lines.append(f"{status} {date_str} · {note}{car} · {days:.0f}d · {pay:.0f}€")

    lines.append(f"\n*Total: {total_days:.1f} dias · {total_pay:.0f}€*")

    if pending:
        lines.append(f"⏳ {len(pending)} serviço(s) ainda a aguardar validação do cliente.")
    else:
        lines.append("💳 Pagamento processado esta semana.")

    return "\n".join(lines)


def send_summary(phone: str, message: str, dry_run: bool = False) -> str | None:
    if dry_run:
        print(f"\n--- DRY RUN → {phone} ---")
        print(message)
        return "DRY_RUN"
    # Import send function from whatsapp_handler
    sys_path_backup = _sys.path[:]
    _sys.path.insert(0, AIOS_ROOT)
    try:
        from bin.whatsapp_handler import send_whatsapp_message
        return send_whatsapp_message(phone, message)
    finally:
        _sys.path[:] = sys_path_backup


def run(week_start: dt.date | None = None, dry_run: bool = False):
    if week_start is None:
        week_start, week_end = last_week_range()
    else:
        week_end = week_start + dt.timedelta(days=6)

    log.info("WEEKLY SUMMARY week=%s–%s dry_run=%s", week_start, week_end, dry_run)

    conn = psycopg2.connect(DSN, cursor_factory=RealDictCursor)
    try:
        entries = get_week_timesheets(conn, week_start, week_end)
        workers = get_active_workers(conn)

        # Group entries by phone
        by_phone: dict[str, list] = {}
        for e in entries:
            phone = e.get("whatsapp_phone") or e.get("worker_phone") or ""
            if phone:
                by_phone.setdefault(phone, []).append(e)

        sent = 0
        for w in workers:
            phone = w["whatsapp_phone"]
            worker_entries = by_phone.get(phone, [])

            if not worker_entries:
                log.info("SKIP %s — sem registos na semana", phone)
                continue

            name    = w["worker_name"]
            message = build_summary(name, worker_entries, week_start, week_end)
            sid     = send_summary(phone, message, dry_run)
            log.info("SENT %s worker=%s sid=%s", phone, name, sid)
            sent += 1

        log.info("DONE sent=%d workers=%d", sent, len(workers))
        print(json.dumps({"ok": True, "sent": sent, "week": str(week_start)}))
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--week", default=None, help="YYYY-MM-DD (monday)")
    args = parser.parse_args()

    week_start = None
    if args.week:
        week_start = dt.date.fromisoformat(args.week)

    run(week_start=week_start, dry_run=args.dry_run)
