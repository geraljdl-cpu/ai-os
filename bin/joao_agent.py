#!/usr/bin/env python3
"""
joao_agent.py — Chief of Staff AI-OS.

Ciclos:
  morning  08:30 — briefing completo + sugestões
  midday   13:00 — alertas urgentes apenas
  evening  18:00 — fecho do dia

Uso:
  python3 bin/joao_agent.py morning
  python3 bin/joao_agent.py midday
  python3 bin/joao_agent.py evening
"""
# Remover bin/ do sys.path (evita shadowing de stdlib)
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys
import urllib.request
import datetime as dt
from typing import Any

import psycopg2
import psycopg2.extras

# ── Config ────────────────────────────────────────────────────────────────────
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios",
).replace("postgresql+pg8000://", "postgresql://")
TG_TOKEN = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT  = os.environ.get("AIOS_TG_CHAT", "")

MAX_NEW_TASKS_PER_DAY = 3
TASK_SCORE_THRESHOLD  = 8   # só sugere twin_task se score >= 8


# ── DB helpers ────────────────────────────────────────────────────────────────
def db() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


def fetch_all(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_one(conn, sql: str, params: tuple = ()) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return None if row is None else dict(row)


def execute(conn, sql: str, params: tuple = ()) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ── load_state ────────────────────────────────────────────────────────────────
def load_state(conn) -> dict[str, Any]:
    """Read all relevant data sources and return normalized state dict."""

    decisions = fetch_all(conn, """
        SELECT id, kind, ref_id, title, status, created_at
        FROM public.decision_queue
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT 20
    """)

    ideas = fetch_all(conn, """
        SELECT id, title, status, created_at, updated_at
        FROM public.idea_threads
        WHERE status IN ('open', 'analyzed')
        ORDER BY updated_at DESC
        LIMIT 20
    """)

    # twin_tasks: case_id is required FK — query existing tasks only (read-only)
    tasks = fetch_all(conn, """
        SELECT id, title, status, due_at, case_id, assignee, updated_at
        FROM public.twin_tasks
        WHERE status NOT IN ('done', 'cancelled')
        ORDER BY due_at ASC NULLS LAST, updated_at ASC
        LIMIT 50
    """)

    obligations = fetch_all(conn, """
        SELECT id, type, entity, label, due_date, amount, status
        FROM public.finance_obligations
        WHERE status IN ('pending', 'approved')
        ORDER BY due_date ASC
        LIMIT 30
    """)

    payouts = fetch_all(conn, """
        SELECT id, worker_id, week_start, total_hours, amount, status
        FROM public.finance_payouts
        WHERE status IN ('pending', 'approved')
        ORDER BY week_start DESC
        LIMIT 20
    """)

    # tenders: twin_entities type='tender', score in metadata jsonb
    tenders = fetch_all(conn, """
        SELECT id, name, metadata
        FROM public.twin_entities
        WHERE type = 'tender'
          AND status != 'dismissed'
        ORDER BY (COALESCE(metadata->>'score','0'))::int DESC, id DESC
        LIMIT 20
    """)

    docs_critical = fetch_all(conn, """
        SELECT id, title, doc_type, owner_type, owner_id, expiry_date, status, sensitivity
        FROM public.documents
        WHERE status IN ('expired', 'expiring')
        ORDER BY expiry_date ASC NULLS LAST
        LIMIT 10
    """)

    doc_requests_open = fetch_all(conn, """
        SELECT id, doc_type, owner_type, owner_id, status, due_date, process_type
        FROM public.document_requests
        WHERE status NOT IN ('done', 'failed')
        ORDER BY due_date ASC NULLS LAST
        LIMIT 10
    """)

    return {
        "decisions":         decisions,
        "ideas":             ideas,
        "tasks":             tasks,
        "obligations":       obligations,
        "payouts":           payouts,
        "tenders":           tenders,
        "docs_critical":     docs_critical,
        "doc_requests_open": doc_requests_open,
    }


# ── detect_urgencies ──────────────────────────────────────────────────────────
def detect_urgencies(state: dict) -> list[dict]:
    """Apply rules, return list of alert dicts sorted by score desc."""
    now    = dt.datetime.now(dt.timezone.utc).date()
    alerts = []

    # Fiscal obligations ≤ 7 days
    for ob in state["obligations"]:
        due = ob.get("due_date")
        if due is None:
            continue
        # psycopg2 returns date objects
        due_d = due if isinstance(due, dt.date) else dt.date.fromisoformat(str(due)[:10])
        days  = (due_d - now).days
        if days <= 7:
            amt = f" — {float(ob['amount']):.0f}€" if ob.get("amount") else ""
            alerts.append({
                "kind":     "obligation",
                "title":    f"{ob['label']}{amt} vence em {days}d",
                "details":  f"entity={ob.get('entity')} amount={ob.get('amount')}",
                "ref_kind": "obligation",
                "ref_id":   str(ob["id"]),
                "score":    10 if days <= 2 else 8,
            })

    # Twin tasks overdue
    for task in state["tasks"]:
        due_at = task.get("due_at")
        if due_at:
            due_d = due_at.date() if hasattr(due_at, "date") else dt.date.fromisoformat(str(due_at)[:10])
            if due_d < now:
                alerts.append({
                    "kind":     "task_overdue",
                    "title":    f"Tarefa atrasada: {task['title']}",
                    "details":  f"task_id={task['id']}",
                    "ref_kind": "task",
                    "ref_id":   str(task["id"]),
                    "score":    8,
                })

    # Blocked tasks
    for task in state["tasks"]:
        if task.get("status") == "blocked":
            alerts.append({
                "kind":     "task_blocked",
                "title":    f"Tarefa bloqueada: {task['title']}",
                "details":  f"task_id={task['id']}",
                "ref_kind": "task",
                "ref_id":   str(task["id"]),
                "score":    9,
            })

    # Pending payouts this week
    this_monday = now - dt.timedelta(days=now.weekday())
    week_pays   = [
        p for p in state["payouts"]
        if str(p.get("week_start", ""))[:10] == this_monday.isoformat()
    ]
    if week_pays:
        total = sum(float(p.get("amount") or 0) for p in week_pays)
        alerts.append({
            "kind":     "payout",
            "title":    f"{len(week_pays)} pagamentos RH pendentes — {total:.0f}€",
            "details":  f"semana {this_monday}",
            "ref_kind": "payout",
            "ref_id":   None,
            "score":    7,
        })

    # Tenders with deadline ≤ 3 days (deadline in metadata)
    for tender in state["tenders"][:10]:
        meta = tender.get("metadata") or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except: meta = {}
        deadline = meta.get("deadline") or meta.get("deadline_date")
        if deadline:
            try:
                dl    = dt.date.fromisoformat(str(deadline)[:10])
                days  = (dl - now).days
                if days <= 3:
                    alerts.append({
                        "kind":     "tender_deadline",
                        "title":    f"Concurso '{tender['name']}' — prazo em {days}d",
                        "details":  f"tender_id={tender['id']} deadline={deadline}",
                        "ref_kind": "tender",
                        "ref_id":   str(tender["id"]),
                        "score":    9,
                    })
            except Exception:
                pass

    # Expired / expiring documents
    for doc in state.get("docs_critical", []):
        exp = doc.get("expiry_date")
        days_left = None
        if exp is not None:
            exp_d = exp if isinstance(exp, dt.date) else dt.date.fromisoformat(str(exp)[:10])
            days_left = (exp_d - now).days
        label = f"{doc['doc_type']} ({doc['owner_type']}#{doc['owner_id']})"
        if doc["status"] == "expired":
            alerts.append({
                "kind":     "doc_expired",
                "title":    f"Doc expirado: {label}",
                "details":  f"doc_id={doc['id']} expiry={exp}",
                "ref_kind": "document",
                "ref_id":   str(doc["id"]),
                "score":    9,
            })
        elif doc["status"] == "expiring" and days_left is not None and days_left <= 7:
            alerts.append({
                "kind":     "doc_expiring",
                "title":    f"Doc expira em {days_left}d: {label}",
                "details":  f"doc_id={doc['id']} expiry={exp}",
                "ref_kind": "document",
                "ref_id":   str(doc["id"]),
                "score":    7,
            })

    # Open doc requests due ≤ 3 days
    for req in state.get("doc_requests_open", []):
        due = req.get("due_date")
        if due:
            due_d = due if isinstance(due, dt.date) else dt.date.fromisoformat(str(due)[:10])
            days_left = (due_d - now).days
            if days_left <= 3:
                alerts.append({
                    "kind":     "doc_request_due",
                    "title":    f"Pedido doc urgente: {req['doc_type']} vence em {days_left}d",
                    "details":  f"req_id={req['id']} process={req.get('process_type')}",
                    "ref_kind": "doc_request",
                    "ref_id":   str(req["id"]),
                    "score":    8,
                })

    return sorted(alerts, key=lambda x: -x["score"])


# ── score_priorities ──────────────────────────────────────────────────────────
def score_priorities(state: dict, alerts: list[dict]) -> list[dict]:
    """
    Produce sorted list of actionable items.
    score = urgência temporal + impacto financeiro + dependência + risco fiscal + alinhamento
    """
    items: list[dict] = []

    # Urgency alerts → top priority
    for a in alerts:
        items.append({
            "source":         a["kind"],
            "title":          a["title"],
            "details":        a.get("details", ""),
            "ref_kind":       a.get("ref_kind"),
            "ref_id":         a.get("ref_id"),
            "priority_score": a["score"],
            "task_type":      "finance_task" if a["kind"] == "obligation" else "ops_task",
        })

    # Pending decisions
    for d in state["decisions"][:10]:
        items.append({
            "source":         "decision",
            "title":          f"Decidir: {d['title']}",
            "details":        f"decision_id={d['id']}",
            "ref_kind":       "decision",
            "ref_id":         str(d["id"]),
            "priority_score": 7,
            "task_type":      "decision_task",
        })

    # Ideas with analyzed status (ready for decision)
    for idea in state["ideas"][:5]:
        score = 6 if idea.get("status") == "analyzed" else 4
        items.append({
            "source":         "idea",
            "title":          f"Rever ideia: {idea['title']}",
            "details":        f"idea_id={idea['id']}",
            "ref_kind":       "idea",
            "ref_id":         str(idea["id"]),
            "priority_score": score,
            "task_type":      "project_task",
        })

    # Top tenders (high radar score)
    for tender in state["tenders"][:5]:
        meta = tender.get("metadata") or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except: meta = {}
        try:
            t_score = int(meta.get("score") or 0)
        except Exception:
            t_score = 0
        if t_score >= 6:
            items.append({
                "source":         "tender",
                "title":          f"Candidatura: {tender['name']}",
                "details":        f"tender_id={tender['id']} score={t_score}",
                "ref_kind":       "tender",
                "ref_id":         str(tender["id"]),
                "priority_score": min(t_score, 6),
                "task_type":      "project_task",
            })

    items.sort(key=lambda x: -x["priority_score"])
    return items


# ── propose_tasks ─────────────────────────────────────────────────────────────
def propose_tasks(conn, priorities: list[dict]) -> list[dict]:
    """
    Select ≤ MAX_NEW_TASKS_PER_DAY items; dedupe against last 48h.
    Only include items with priority_score >= 7.
    """
    today_count = (fetch_one(conn, """
        SELECT COUNT(*) AS n FROM public.agent_suggestions
        WHERE kind = 'task_suggestion' AND created_at::date = CURRENT_DATE
    """) or {}).get("n", 0)

    remaining = max(0, MAX_NEW_TASKS_PER_DAY - int(today_count))
    if remaining == 0:
        return []

    out: list[dict] = []
    for item in priorities:
        if len(out) >= remaining:
            break
        if item["priority_score"] < 7:
            break  # sorted desc — nothing below will qualify

        title  = item["title"]
        ref_id = item.get("ref_id") or ""

        exists = fetch_one(conn, """
            SELECT 1 FROM public.agent_suggestions
            WHERE kind = 'task_suggestion'
              AND title = %s
              AND COALESCE(ref_id, '') = %s
              AND created_at >= now() - interval '48 hours'
            LIMIT 1
        """, (title, ref_id))
        if exists:
            continue

        out.append(item)

    return out


# ── insert_suggestion ─────────────────────────────────────────────────────────
def insert_suggestion(conn, kind: str, title: str, details: str = "",
                      ref_kind: str | None = None, ref_id: str | None = None,
                      score: int = 0) -> None:
    execute(conn, """
        INSERT INTO public.agent_suggestions(kind, title, details, ref_kind, ref_id, score)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (kind, title[:500], (details or "")[:1000], ref_kind, ref_id, score))


# ── build_briefing ────────────────────────────────────────────────────────────
def build_briefing(priorities: list[dict], alerts: list[dict],
                   task_suggestions: list[dict], mode: str) -> str:
    today = dt.date.today().strftime("%d/%m/%Y")
    label = {"morning": "🌅 Bom dia", "midday": "☀ Meio-dia", "evening": "🌆 Fim do dia"}[mode]

    top3     = priorities[:3]
    top_decs = [p for p in priorities if p["source"] == "decision"][:2]

    lines = [f"{label} — {today}", ""]

    lines.append("Prioridades:")
    if top3:
        for i, p in enumerate(top3, 1):
            lines.append(f"  {i}. {p['title']}")
    else:
        lines.append("  (nenhuma)")

    lines.append("")
    lines.append("Decisões:")
    if top_decs:
        for d in top_decs:
            lines.append(f"  - {d['title']}")
    else:
        lines.append("  - Nenhuma decisão pendente relevante")

    lines.append("")
    lines.append("Alertas:")
    if alerts:
        for a in alerts[:4]:
            lines.append(f"  - {a['title']}")
    else:
        lines.append("  - Sem alertas críticos")

    if task_suggestions and mode in ("morning", "evening"):
        lines.append("")
        lines.append(f"Sugestões ({len(task_suggestions)}):")
        for t in task_suggestions:
            lines.append(f"  → {t['title']}")

    return "\n".join(lines)


# ── write_events ──────────────────────────────────────────────────────────────
def write_events(conn, briefing: str, task_suggestions: list[dict],
                 alerts: list[dict], mode: str) -> None:
    """Insert joao_agent_* events using the correct events schema."""
    now = dt.datetime.now(dt.timezone.utc)

    execute(conn, """
        INSERT INTO public.events(ts, level, source, kind, message, data)
        VALUES (%s, 'info', 'joao_agent', 'joao_agent_run', %s, %s::jsonb)
    """, (now, f"joao_agent {mode}", json.dumps({
        "mode": mode,
        "alerts": len(alerts),
        "suggestions": len(task_suggestions),
    })))

    for a in alerts[:5]:
        execute(conn, """
            INSERT INTO public.events(ts, level, source, kind, message, data)
            VALUES (%s, 'warn', 'joao_agent', 'joao_agent_alerted', %s, %s::jsonb)
        """, (now, a["title"], json.dumps({
            "ref_kind": a.get("ref_kind"),
            "ref_id":   a.get("ref_id"),
        })))

    for t in task_suggestions:
        execute(conn, """
            INSERT INTO public.events(ts, level, source, kind, message, data)
            VALUES (%s, 'info', 'joao_agent', 'joao_agent_suggested', %s, %s::jsonb)
        """, (now, t["title"], json.dumps({
            "score":    t.get("priority_score"),
            "ref_kind": t.get("ref_kind"),
        })))

    # Save briefing to agent_suggestions
    execute(conn, """
        INSERT INTO public.agent_suggestions(kind, title, details, score)
        VALUES ('briefing', %s, %s, 0)
    """, (f"Briefing {mode} {dt.date.today().isoformat()}", briefing))


# ── send_notifications ────────────────────────────────────────────────────────
def send_notifications(mode: str, briefing: str) -> None:
    print(f"[joao_agent:{mode}]\n{briefing}")
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        body = json.dumps({
            "chat_id":    TG_CHAT,
            "text":       briefing,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[joao_agent] telegram error: {e}", file=sys.stderr)


# ── run_cycle ─────────────────────────────────────────────────────────────────
def run_cycle(mode: str) -> int:
    conn = db()
    try:
        state     = load_state(conn)
        alerts    = detect_urgencies(state)
        priorities = score_priorities(state, alerts)

        # midday: no new task proposals — alerts only
        task_suggestions: list[dict] = []
        if mode in ("morning", "evening"):
            task_suggestions = propose_tasks(conn, priorities)

        briefing = build_briefing(priorities, alerts, task_suggestions, mode)

        # Persist
        for a in alerts:
            insert_suggestion(conn, "alert", a["title"], a.get("details", ""),
                              a.get("ref_kind"), a.get("ref_id"), a.get("score", 0))

        for s in task_suggestions:
            insert_suggestion(conn, "task_suggestion", s["title"], s.get("details", ""),
                              s.get("ref_kind"), s.get("ref_id"), s.get("priority_score", 0))

        write_events(conn, briefing, task_suggestions, alerts, mode)
        conn.commit()

        send_notifications(mode, briefing)
        return 0

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback; traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        conn.close()


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode not in ("morning", "midday", "evening"):
        print("Usage: joao_agent.py [morning|midday|evening]", file=sys.stderr)
        return 2
    return run_cycle(mode)


if __name__ == "__main__":
    raise SystemExit(main())
