#!/usr/bin/env python3
"""
finance_tick.py — Finance & obligations monitor.
Corre via pipeline_scheduler a cada ~1h.

Verifica:
  - obrigações fiscais vencidas → incident crit
  - obrigações nos próximos 7 dias → incident warn
  - pagamentos RH pendentes → incident warn
  - fecha incidentes quando condição resolvida
"""
import sys, os, json

_bin_dir = os.path.dirname(os.path.abspath(__file__))
if _bin_dir in sys.path:
    sys.path.remove(_bin_dir)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _ensure_incident(conn, text, source, kind, severity, title, details=None):
    existing = conn.execute(text("""
        SELECT id FROM public.incidents
        WHERE source=:s AND kind=:k AND status='open'
          AND created_at > NOW() - INTERVAL '4 hours'
        LIMIT 1
    """), {"s": source, "k": kind}).mappings().first()
    if existing:
        return False
    conn.execute(text("""
        INSERT INTO public.incidents (source, kind, severity, title, details)
        VALUES (:src, :kind, :sev, :title, :details)
    """), {"src": source, "kind": kind, "sev": severity,
           "title": title, "details": details})
    return True


def _auto_resolve(conn, text, source, kind):
    conn.execute(text("""
        UPDATE public.incidents
        SET status='resolved', resolved_at=NOW()
        WHERE source=:s AND kind=:k AND status='open'
    """), {"s": source, "k": kind})


def check_obligations(engine, text):
    with engine.begin() as conn:
        overdue = conn.execute(text("""
            SELECT label, due_date FROM public.finance_obligations
            WHERE status = 'pending' AND due_date < CURRENT_DATE
            ORDER BY due_date
        """)).mappings().all()
        if overdue:
            titles = ", ".join(f"{r['label']} ({str(r['due_date'])[:10]})" for r in overdue)
            created = _ensure_incident(conn, text, "finance", "obligation_overdue", "crit",
                                       f"{len(overdue)} obrigação(ões) fiscal(ais) vencida(s)", titles)
            if created:
                print(f"[finance_tick] CRIT: {len(overdue)} obrigações vencidas")
        else:
            _auto_resolve(conn, text, "finance", "obligation_overdue")

        upcoming = conn.execute(text("""
            SELECT label, due_date FROM public.finance_obligations
            WHERE status = 'pending'
              AND due_date >= CURRENT_DATE
              AND due_date <= CURRENT_DATE + INTERVAL '7 days'
            ORDER BY due_date
        """)).mappings().all()
        if upcoming:
            titles = ", ".join(f"{r['label']} ({str(r['due_date'])[:10]})" for r in upcoming)
            _ensure_incident(conn, text, "finance", "obligation_soon", "warn",
                             f"{len(upcoming)} obrigação(ões) nos próximos 7 dias", titles)
        else:
            _auto_resolve(conn, text, "finance", "obligation_soon")


def check_payouts(engine, text):
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total
            FROM public.finance_payouts
            WHERE status IN ('pending', 'approved')
              AND week_start >= CURRENT_DATE - INTERVAL '14 days'
        """)).mappings().first()
        cnt   = int(row["n"] or 0)
        total = float(row["total"] or 0)
        if cnt > 0:
            _ensure_incident(conn, text, "finance", "payouts_pending", "warn",
                             f"{cnt} pagamento(s) RH pendentes — €{total:.0f}",
                             "Aceder a /finance para aprovar")
        else:
            _auto_resolve(conn, text, "finance", "payouts_pending")


def run():
    engine, text = _conn()
    results = {}
    for fn in [check_obligations, check_payouts]:
        try:
            fn(engine, text)
            results[fn.__name__] = "ok"
        except Exception as e:
            print(f"[finance_tick] erro em {fn.__name__}: {e}", file=sys.stderr)
            results[fn.__name__] = str(e)
    print(json.dumps({"ok": True, "checks": results}))


if __name__ == "__main__":
    run()
