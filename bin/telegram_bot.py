#!/usr/bin/env python3
import os, time, json, subprocess, datetime
from pathlib import Path

import requests

AIOS_ROOT = Path(os.environ.get("AIOS_ROOT", str(Path.home() / "ai-os")))
ENV_DB = Path(os.environ.get("AIOS_ENV_DB", str(Path.home() / ".env.db")))

def load_env_db():
    if ENV_DB.exists():
        for line in ENV_DB.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def load_env_file(path: str):
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

load_env_db()
load_env_file("/etc/aios.env")  # OPS token

TG_TOKEN  = os.environ.get("AIOS_TG_TOKEN", "").strip()
TG_CHAT   = os.environ.get("AIOS_TG_CHAT", "").strip()
OPS_TOKEN = os.environ.get("AIOS_OPS_TOKEN", "").strip()
API_BASE  = f"https://api.telegram.org/bot{TG_TOKEN}"
UI_BASE   = os.environ.get("AIOS_UI_BASE", "http://127.0.0.1:3000").rstrip("/")
PGID_CMD = "docker ps --format '{{.ID}} {{.Image}}' | grep postgres | head -1 | awk '{print $1}'"

if not TG_TOKEN or not TG_CHAT:
    raise SystemExit("Missing AIOS_TG_TOKEN or AIOS_TG_CHAT in ~/.env.db")

def sh(cmd: str) -> str:
    return subprocess.check_output(["bash", "-lc", cmd], text=True).strip()

def nowz():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")

def tg(method: str, payload: dict):
    r = requests.post(f"{API_BASE}/{method}", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def send(text: str, reply_markup=None):
    payload = {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def edit(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

def answer_cb(cb_id, text):
    return tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": False})

def get_pg_id() -> str:
    return sh(PGID_CMD)

def psql(q: str) -> str:
    pgid = get_pg_id()
    qq = q.replace('"', '\\"')
    return sh(f'docker exec -i {pgid} psql -U aios_user -d aios -t -A -c "{qq}"')

def insert_event(level: str, kind: str, message: str, entity_id=None, data=None):
    try:
        data_json = json.dumps(data or {}, ensure_ascii=False).replace("'", "''")
        ent = "NULL" if entity_id is None else str(int(entity_id))
        msg = message.replace("'", "''")[:500]
        psql(f"INSERT INTO public.events (ts, level, source, kind, message, entity_id, data) "
             f"VALUES (NOW(), '{level}', 'telegram', '{kind}', '{msg}', {ent}, '{data_json}'::jsonb)")
    except Exception:
        pass

def status_text():
    try:
        r = requests.get(f"{UI_BASE}/api/syshealth", timeout=10)
        js = r.json()
        ok = js.get("ok", False)
    except Exception as e:
        return f"[{nowz()}] STATUS\nUI/syshealth ERROR: {e}"

    timers = sh(
        "systemctl list-timers --all | grep -E 'aios-(autopilot|watchdog|telemetry|alerts|worker-heartbeat|pgbackup)' "
        "| awk '{print $1, $5}' || true"
    )
    try:
        w = requests.get(f"{UI_BASE}/api/workers", timeout=10).json()
        online = sum(1 for x in w if (x.get("age_secs") or 999999) < 90)
        total  = len(w)
    except Exception:
        online = 0; total = 0

    head = "OK" if ok else "FAIL"
    return (
        f"[{nowz()}] AI-OS {head}\n"
        f"Workers: {online}/{total} online\n\n"
        f"Timers:\n{timers or '(sem dados)'}\n\n"
        f"NOC: {UI_BASE}/ops"
    )

def list_approvals(limit=10):
    rows = psql(
        "SELECT id, action, status, coalesce(summary,''), requested_at "
        f"FROM public.twin_approvals WHERE status='pending' ORDER BY requested_at ASC LIMIT {int(limit)};"
    )
    items = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        aid, action, status, summary, requested_at = [p.strip() for p in parts[:5]]
        items.append({"id": aid, "action": action, "summary": summary, "requested_at": requested_at})
    return items

def approvals_message(items):
    if not items:
        return f"[{nowz()}] Approvals\n(nenhum pendente)"
    lines = [f"[{nowz()}] Approvals pendentes ({len(items)}):"]
    for it in items:
        summary = f" — {it['summary']}" if it['summary'] else ""
        lines.append(f"  #{it['id']} {it['action']}{summary}")
    return "\n".join(lines)

def approvals_keyboard(items):
    if not items:
        return None
    kb = []
    for it in items[:8]:
        aid = it["id"]
        kb.append([
            {"text": f"✅ #{aid}", "callback_data": f"appr:approve:{aid}"},
            {"text": f"❌ #{aid}", "callback_data": f"appr:reject:{aid}"},
        ])
    return {"inline_keyboard": kb}

def set_approval(aid: int, new_status: str):
    psql(f"UPDATE public.twin_approvals SET status='{new_status}', decided_at=NOW() WHERE id={int(aid)};")
    insert_event("info", "twin_approval", f"approval #{aid} => {new_status}",
                 data={"approval_id": int(aid), "status": new_status})

def ui_post(path: str, body: dict = None, ops_auth: bool = False) -> dict:
    headers = {"Content-Type": "application/json"}
    if ops_auth and OPS_TOKEN:
        headers["X-AIOS-OPS-TOKEN"] = OPS_TOKEN
    r = requests.post(f"{UI_BASE}/api{path}", json=body or {}, headers=headers, timeout=20)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "output": r.text[:300]}

def handle_do(args: list) -> str:
    if not args:
        return "Uso: /do <health|tick|watchdog|enqueue <kind> [target]>"

    action = args[0].lower()
    insert_event("info", "telegram_do", f"/do {' '.join(args)}")

    if action == "health":
        r = ui_post("/actions/healthcheck")
        out = r.get("output", "")[:600]
        return f"/do health\n{out}"

    if action == "tick":
        r = ui_post("/actions/tick")
        ok = "" if r.get("ok") else ""
        return f"/do tick {ok}\n{r.get('output','')[:200]}"

    if action == "watchdog":
        r = ui_post("/actions/watchdog")
        ok = "" if r.get("ok") else ""
        return f"/do watchdog {ok}\n{r.get('output','')[:200]}"

    if action == "enqueue":
        if len(args) < 2:
            return "Uso: /do enqueue <kind> [target_worker_id]"
        kind   = args[1]
        target = args[2] if len(args) > 2 else None
        body   = {"kind": kind, "payload": {}, "target_worker_id": target}
        r = ui_post("/worker_jobs/enqueue", body, ops_auth=True)
        if r.get("ok"):
            return f"/do enqueue {kind} → job #{r.get('job_id','?')} criado"
        return f"/do enqueue {kind} ERRO: {r.get('error', r)}"

    return f"Ação desconhecida: {action}\nDisponíveis: health, tick, watchdog, enqueue"

ESTADOS_PT = {
    "agendado":        "Agendado",
    "chegou":          "Chegou à fábrica",
    "em_processamento":"Em processamento",
    "separacao":       "Separação cobre/plástico",
    "concluido":       "Concluído",
    "pronto_levantar": "Pronto a levantar",
    "faturado":        "Faturado",
    "fechado":         "Fechado",
}

def get_approval_context(aid: int) -> dict:
    try:
        rows = psql(f"SELECT action, context FROM public.twin_approvals WHERE id={int(aid)} LIMIT 1;")
        for line in rows.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 1)
            action = parts[0].strip()
            ctx = json.loads(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else {}
            return {"action": action, "context": ctx}
    except Exception:
        pass
    return {}

def handle_lote(args: list) -> str:
    if not args:
        return "Uso:\n/lote novo <kg> <cliente>\n/lote ver <id>\n/lote avancar <id> [nota]"

    sub = args[0].lower()

    if sub == "ver":
        if len(args) < 2:
            return "Uso: /lote ver <id>"
        r = requests.get(f"{UI_BASE}/api/twin/batch/{args[1]}", timeout=15).json()
        if r.get("error"):
            return f"Erro: {r['error']}"
        estado_pt   = ESTADOS_PT.get(r.get("estado",""), r.get("estado","?"))
        proximo     = r.get("proximo_estado")
        proximo_pt  = ESTADOS_PT.get(proximo, proximo) if proximo else "—"
        tasks_pend  = [t for t in r.get("tasks", []) if t["status"] == "pending"]
        tasks_done  = [t for t in r.get("tasks", []) if t["status"] == "done"]
        return (
            f"Lote #{r['entity_id']} — {r.get('client','?')}\n"
            f"  Peso: {r.get('kg','?')}kg\n"
            f"  Estado: {estado_pt}\n"
            f"  Próximo: {proximo_pt}\n"
            f"  Tasks concluídas: {len(tasks_done)}/{len(r.get('tasks',[]))}\n"
            f"  Próxima tarefa: {tasks_pend[0]['title'] if tasks_pend else '(nenhuma)'}\n"
            + (f"\nPara avançar: /lote avancar {r['entity_id']}" if proximo else "")
        )

    if sub == "avancar":
        if len(args) < 2:
            return "Uso: /lote avancar <id> [nota]"
        nota = " ".join(args[2:]) if len(args) > 2 else ""
        r = ui_post(f"/twin/batch/{args[1]}/advance", {"nota": nota}, ops_auth=True)
        if not r.get("ok"):
            return f"Erro: {r.get('error', r)}"
        de_pt  = ESTADOS_PT.get(r.get("de",""), r.get("de","?"))
        para_pt = ESTADOS_PT.get(r.get("para",""), r.get("para","?"))
        insert_event("info", "telegram_lote", f"Lote #{args[1]} avançado: {r.get('de')} → {r.get('para')}")
        return f"Lote #{r['entity_id']} avançado\n  {de_pt} → {para_pt}"

    if sub == "faturar":
        if len(args) < 3:
            return "Uso: /lote faturar <id> <preco_por_kg>\nEx: /lote faturar 2 0.45"
        try:
            preco = float(args[2].replace(",", "."))
        except ValueError:
            return f"Preço inválido: {args[2]}"
        r = ui_post(f"/twin/batch/{args[1]}/faturar", {"preco_kg": preco}, ops_auth=True)
        if not r.get("ok"):
            return f"Erro: {r.get('error', r)}"
        insert_event("info", "telegram_lote", f"Lote #{args[1]} faturação pendente: €{r.get('valor')}",
                     data={"entity_id": args[1], "valor": r.get("valor"), "approval_id": r.get("approval_id")})
        return (
            f"Faturação pendente — Lote #{r['entity_id']}\n"
            f"  {r.get('summary','')}\n\n"
            f"Use /approvals para aprovar."
        )

    if sub == "fechar":
        if len(args) < 2:
            return "Uso: /lote fechar <id>"
        r = ui_post(f"/twin/batch/{args[1]}/advance", {"nota": "caso fechado"}, ops_auth=True)
        if not r.get("ok"):
            return f"Erro: {r.get('error', r)}"
        insert_event("info", "telegram_lote", f"Lote #{args[1]} fechado")
        para_pt = ESTADOS_PT.get(r.get("para", ""), r.get("para", "?"))
        return f"Lote #{r.get('entity_id', args[1])} → {para_pt} ✓"

    if sub == "resultado":
        if len(args) < 4:
            return "Uso: /lote resultado <id> <kg_cobre> <kg_plastico>"
        try:
            kg_c = float(args[2].replace(",", "."))
            kg_p = float(args[3].replace(",", "."))
        except ValueError:
            return "Kg inválido"
        r = ui_post(f"/twin/batch/{args[1]}/resultado", {"kg_cobre": kg_c, "kg_plastico": kg_p}, ops_auth=True)
        if not r.get("ok"):
            return f"Erro: {r.get('error', r)}"
        insert_event("info", "telegram_lote", f"Lote #{args[1]} resultado: {kg_c}kg cobre, {kg_p}kg plástico",
                     data={"entity_id": args[1], "kg_cobre": kg_c, "kg_plastico": kg_p})
        residuo = r.get("kg_residuo", "?")
        return (
            f"Resultado registado — Lote #{r['entity_id']}\n"
            f"  Cobre:    {kg_c}kg\n"
            f"  Plástico: {kg_p}kg\n"
            f"  Resíduo:  {residuo}kg\n"
            f"\nPara avançar: /lote avancar {args[1]}"
        )

    if sub != "novo":
        return "Uso:\n/lote novo <kg> <cliente>\n/lote ver <id>\n/lote avancar <id> [nota]\n/lote resultado <id> <kg_cobre> <kg_plastico>\n/lote faturar <id> <preco_kg>\n/lote fechar <id>"
    if len(args) < 3:
        return "Uso: /lote novo <kg> <cliente>\nEx: /lote novo 1200 MetalX"
    try:
        kg = float(args[1].replace(",", "."))
    except ValueError:
        return f"Kg inválido: {args[1]}"
    client = " ".join(args[2:])
    r = ui_post("/twin/cable_batch", {"kg": kg, "client": client}, ops_auth=True)
    if not r.get("ok"):
        return f"Erro ao criar lote: {r.get('error', r)}"
    insert_event("info", "telegram_lote", f"Lote criado: {client} {kg}kg",
                 data={"entity_id": r.get("entity_id"), "case_id": r.get("case_id")})
    return (
        f"Lote criado\n"
        f"  ID: #{r['entity_id']} | Case: #{r['case_id']}\n"
        f"  Cliente: {r['client']}\n"
        f"  Peso: {r['kg']}kg\n"
        f"  Estado: {r['estado']}\n"
        f"  Ver estado: /lote ver {r['entity_id']}\n"
        + (f"  Tracking cliente: {UI_BASE}/lote/{r['client_token']}" if r.get('client_token') else "")
    )

def handle_tenders(args: list) -> str:
    sub = args[0] if args else "top"

    if sub == "estado" and len(args) >= 3:
        eid    = args[1]
        estado = args[2]
        r = ui_post(f"/twin/tender/{eid}/estado", {"estado": estado}, ops_auth=True)
        if r.get("ok"):
            return f"Tender #{eid} atualizado para '{estado}'."
        return f"Erro: {r.get('error', 'desconhecido')}"

    # default: top tenders
    try:
        r = requests.get(f"{UI_BASE}/api/twin/tenders?limit=10", timeout=15).json()
    except Exception as e:
        return f"Erro ao carregar tenders: {e}"
    if not isinstance(r, list) or not r:
        return "Nenhum tender detectado ainda. Executa o radar:\npython3 ~/ai-os/bin/radar_ted.py"
    lines = [f"Radar TED — top {min(len(r),10)} concursos:\n"]
    for t in r[:10]:
        score = t.get("score", 0)
        title = (t.get("title") or "")[:60]
        dl    = t.get("deadline") or "—"
        estado = t.get("estado") or "novo"
        pub   = t.get("pub_num") or ""
        lines.append(f"[{score}] {title}\n  Ref: {pub}  Prazo: {dl}  Estado: {estado}")
    lines.append(f"\nVer todos: {UI_BASE}/tenders")
    return "\n".join(lines)


def handle_idea(args: list) -> str:
    """Cria nova ideia via Telegram: /idea título da ideia"""
    if not args:
        return "Uso: /idea <texto da ideia>\nEx: /idea app de tracking para eventos"
    title = " ".join(args)
    try:
        r = requests.post(
            f"{UI_BASE}/api/ideas",
            json={"title": title, "message": title},
            headers={"Authorization": f"Bearer {_get_ops_token()}"},
            timeout=10,
        )
        d = r.json()
        if d.get("ok"):
            return (
                f"💡 Ideia #{d['id']} guardada!\n\n"
                f"*{title}*\n\n"
                f"Para analisar com o Conselho de IA:\n"
                f"  Acede a {UI_BASE}/joao e clica 'Analisar'"
            )
        return f"Erro: {d.get('error', 'desconhecido')}"
    except Exception as e:
        return f"Erro: {e}"


def _get_ops_token() -> str:
    """Lê OPS token do ficheiro de ambiente."""
    for path in ["/etc/aios.env", os.path.expanduser("~/.env.db")]:
        try:
            for line in open(path).readlines():
                if line.startswith("AIOS_OPS_TOKEN="):
                    return line.strip().split("=", 1)[1]
        except Exception:
            pass
    return ""


# ── Cluster agents (/cluster) ─────────────────────────────────────────────────

def handle_cluster(_args) -> str:
    try:
        r = requests.get(f"{UI_BASE}/api/agent/status", timeout=10).json()
    except Exception as e:
        return f"Erro ao obter estado dos agentes: {e}"
    if not isinstance(r, list) or not r:
        return "Sem dados de agentes disponíveis."
    lines = [f"🤖 *Agent Team* — {nowz()}", ""]
    for a in r:
        offline = (a.get("age_secs") or 999999) > 120
        busy    = not offline and a.get("running", 0) > 0
        icon    = "🔴" if offline else "🟡" if busy else "🟢"
        st      = "OFFLINE" if offline else "BUSY" if busy else "IDLE"
        fails   = a.get("jobs_failed", 0)
        fail_s  = f" ⚠️{fails}err" if fails else ""
        last    = a.get("last_kind") or "—"
        lines.append(f"{icon} *{a['agent']}* [{st}] — {a.get('jobs_24h',0)}j/24h{fail_s}\n   _{a.get('agent_desc','')}_ | último: {last}")
    lines.append(f"\n📺 {UI_BASE}/ops")
    return "\n".join(lines)


# ── Alertas de incidentes (/alertas) ──────────────────────────────────────────

def handle_alertas(_args) -> str:
    try:
        r = requests.get(f"{UI_BASE}/api/incidents", timeout=10).json()
    except Exception as e:
        return f"Erro ao obter incidentes: {e}"
    if not isinstance(r, list):
        return "Sem dados de incidentes."
    open_inc = [i for i in r if i.get("status") == "open"]
    if not open_inc:
        return f"✅ Sem incidentes abertos — {nowz()}"
    crits = [i for i in open_inc if i.get("severity") == "crit"]
    warns = [i for i in open_inc if i.get("severity") == "warn"]
    lines = [f"🚨 *Incidentes* — {nowz()}", ""]
    for i in crits:
        lines.append(f"🔴 *CRIT* — {i.get('title','')} [{i.get('source','')}]")
    for i in warns:
        lines.append(f"🟠 *WARN* — {i.get('title','')} [{i.get('source','')}]")
    infos = [i for i in open_inc if i.get("severity") == "info"]
    for i in infos:
        lines.append(f"🔵 *INFO* — {i.get('title','')} [{i.get('source','')}]")
    lines.append(f"\n📺 {UI_BASE}/ops")
    return "\n".join(lines)


# ── Chief of Staff — /joao, /aprovar, /rejeitar, /control ────────────────────

def list_agent_suggestions(limit=8):
    rows = psql(
        "SELECT id, kind, title, score, is_read FROM public.agent_suggestions "
        "WHERE kind IN ('alert','task_suggestion') AND is_read = FALSE "
        f"ORDER BY score DESC, created_at DESC LIMIT {int(limit)};"
    )
    items = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        sid, kind, title, score, is_read = [p.strip() for p in parts[:5]]
        items.append({"id": sid, "kind": kind, "title": title, "score": score})
    return items


def get_agent_briefing() -> str:
    try:
        row = psql(
            "SELECT details FROM public.agent_suggestions "
            "WHERE kind = 'briefing' ORDER BY created_at DESC LIMIT 1;"
        )
        return row.strip() or "(sem briefing)"
    except Exception:
        return "(erro ao carregar briefing)"


def joao_message(suggestions, briefing_snippet=""):
    lines = [f"🤖 *Painel João* — {nowz()}", ""]
    if briefing_snippet:
        # show first 3 lines of briefing only
        snippet = "\n".join(briefing_snippet.splitlines()[:5])
        lines.append(snippet)
        lines.append("")
    if not suggestions:
        lines.append("✅ Sem alertas ou sugestões pendentes")
    else:
        lines.append(f"📋 *Sugestões pendentes ({len(suggestions)}):*")
        for s in suggestions:
            score = s.get("score", "?")
            icon = "🔴" if int(score or 0) >= 9 else "🟠" if int(score or 0) >= 7 else "🟡"
            lines.append(f"{icon} #{s['id']} — {s['title']}")
        lines.append("")
        lines.append("Usa os botões para aprovar/rejeitar.")
    lines.append(f"\n🌐 {UI_BASE}/joao | 📺 {UI_BASE}/control")
    return "\n".join(lines)


def joao_keyboard(suggestions):
    if not suggestions:
        return None
    kb = []
    for s in suggestions[:6]:
        sid = s["id"]
        label = s["title"][:28] + ("…" if len(s["title"]) > 28 else "")
        kb.append([
            {"text": f"✅ #{sid} {label}", "callback_data": f"joao:approve:{sid}"},
            {"text": f"❌ Rejeitar", "callback_data": f"joao:reject:{sid}"},
        ])
    kb.append([{"text": "↻ Actualizar", "callback_data": "joao:refresh"}])
    return {"inline_keyboard": kb}


def handle_joao(_args) -> str:
    suggs   = list_agent_suggestions(6)
    briefing = get_agent_briefing()
    return joao_message(suggs, briefing)


def mark_suggestion_read(sid: int):
    psql(f"UPDATE public.agent_suggestions SET is_read=TRUE WHERE id={int(sid)};")


def approve_suggestion(sid: int):
    """Marca como lida e cria entrada na decision_queue."""
    row = psql(
        f"SELECT title, ref_kind, ref_id FROM public.agent_suggestions WHERE id={int(sid)} LIMIT 1;"
    )
    title, ref_kind, ref_id = "", "", ""
    for line in row.splitlines():
        if line.strip():
            parts = line.split("|")
            title    = parts[0].strip() if len(parts) > 0 else ""
            ref_kind = parts[1].strip() if len(parts) > 1 else ""
            ref_id   = parts[2].strip() if len(parts) > 2 else ""
            break
    mark_suggestion_read(sid)
    if title:
        safe_title = title.replace("'", "''")[:200]
        psql(
            f"INSERT INTO public.decision_queue (kind, ref_id, title, status) "
            f"VALUES ('agent_suggestion', '{sid}', '{safe_title}', 'pending');"
        )
    insert_event("info", "joao_agent_approved", f"sugestão #{sid} aprovada → decision_queue",
                 data={"suggestion_id": sid, "ref_kind": ref_kind, "ref_id": ref_id})


def handle_command(text: str):
    t = (text or "").strip()
    if t.startswith("/status"):
        send(status_text())
        return
    if t.startswith("/approvals"):
        items = list_approvals(10)
        send(approvals_message(items), reply_markup=approvals_keyboard(items))
        return
    if t.startswith("/do"):
        args = t.split()[1:]
        send(handle_do(args))
        return
    if t.startswith("/lote"):
        args = t.split()[1:]
        send(handle_lote(args))
        return
    if t.startswith("/tenders"):
        args = t.split()[1:]
        send(handle_tenders(args))
        return
    if t.startswith("/idea"):
        args = t.split()[1:]
        send(handle_idea(args))
        return
    if t.startswith("/joao"):
        args = t.split()[1:]
        suggs = list_agent_suggestions(6)
        briefing = get_agent_briefing()
        send(joao_message(suggs, briefing), reply_markup=joao_keyboard(suggs))
        return
    if t.startswith("/aprovar"):
        parts = t.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send("Uso: /aprovar <id>\nEx: /aprovar 5"); return
        sid = int(parts[1])
        try:
            approve_suggestion(sid)
            send(f"✅ Sugestão #{sid} aprovada e criada em Decisões Pendentes.\n\nVer: {UI_BASE}/joao")
        except Exception as e:
            send(f"Erro: {e}")
        return
    if t.startswith("/rejeitar"):
        parts = t.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send("Uso: /rejeitar <id>\nEx: /rejeitar 5"); return
        sid = int(parts[1])
        try:
            mark_suggestion_read(sid)
            insert_event("info", "joao_agent_rejected", f"sugestão #{sid} rejeitada via Telegram",
                         data={"suggestion_id": sid})
            send(f"❌ Sugestão #{sid} rejeitada e arquivada.")
        except Exception as e:
            send(f"Erro: {e}")
        return
    if t.startswith("/cluster"):
        send(handle_cluster(t.split()[1:]))
        return
    if t.startswith("/alertas"):
        send(handle_alertas(t.split()[1:]))
        return
    if t.startswith("/control"):
        send(
            f"📺 *Control Room*\n{UI_BASE}/control\n\n"
            f"• 6 zonas em tempo real\n"
            f"• Refresh automático 12s\n"
            f"• Fullscreen: F11"
        )
        return
    if t.startswith("/help") or t == "/start":
        send(
            "Comandos:\n"
            "/joao — briefing + sugestões do agente\n"
            "/aprovar <id> — aprovar sugestão\n"
            "/rejeitar <id> — rejeitar sugestão\n"
            "/cluster — estado da equipa de agentes IA\n"
            "/alertas — incidentes abertos\n"
            "/control — link para o control room\n"
            "/status — estado da infra\n"
            "/approvals — aprovações da fábrica\n"
            "/idea <texto> — nova ideia\n"
            "/tenders — radar de concursos\n"
            "/lote ver|novo|avancar|resultado|faturar|fechar\n"
            "/do health|tick|watchdog|enqueue\n"
            f"\n🌐 {UI_BASE}/joao"
        )
        return
    send("Comando desconhecido. Usa /help")

def handle_callback(cb):
    cb_id      = cb["id"]
    data       = cb.get("data", "")
    msg        = cb.get("message", {})
    chat_id    = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    # ── joao: callbacks (Chief of Staff) ───────────────────────────────────────
    if data.startswith("joao:"):
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""

        if action == "refresh":
            answer_cb(cb_id, "A actualizar...")
            suggs = list_agent_suggestions(6)
            briefing = get_agent_briefing()
            try:
                edit(chat_id, message_id, joao_message(suggs, briefing),
                     reply_markup=joao_keyboard(suggs) or {"inline_keyboard": []})
            except Exception:
                pass
            return

        sid_str = parts[2] if len(parts) > 2 else ""
        if not sid_str.isdigit():
            answer_cb(cb_id, "id inválido"); return
        sid = int(sid_str)

        if action == "approve":
            try:
                approve_suggestion(sid)
                answer_cb(cb_id, f"✅ #{sid} aprovado!")
            except Exception as e:
                answer_cb(cb_id, f"Erro: {e}"); return

        elif action == "reject":
            try:
                mark_suggestion_read(sid)
                insert_event("info", "joao_agent_rejected", f"sugestão #{sid} rejeitada",
                             data={"suggestion_id": sid})
                answer_cb(cb_id, f"❌ #{sid} rejeitado")
            except Exception as e:
                answer_cb(cb_id, f"Erro: {e}"); return

        # refresh inline keyboard after action
        suggs = list_agent_suggestions(6)
        briefing = get_agent_briefing()
        try:
            edit(chat_id, message_id, joao_message(suggs, briefing),
                 reply_markup=joao_keyboard(suggs) or {"inline_keyboard": []})
        except Exception:
            pass
        return

    if not data.startswith("appr:"):
        answer_cb(cb_id, "ok")
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        answer_cb(cb_id, "invalid")
        return

    _, action, aid = parts
    if action not in ("approve", "reject"):
        answer_cb(cb_id, "invalid")
        return

    new_status = "approved" if action == "approve" else "rejected"
    try:
        set_approval(int(aid), new_status)
        # auto-trigger: se é approval de faturação, marca lote como faturado
        if new_status == "approved":
            appr = get_approval_context(int(aid))
            if appr.get("action") == "faturar_lote":
                eid = appr.get("context", {}).get("entity_id")
                if eid:
                    try:
                        ui_post(f"/twin/batch/{eid}/faturar_ok", {}, ops_auth=True)
                    except Exception:
                        pass
        answer_cb(cb_id, new_status)
        items = list_approvals(10)
        edit(chat_id, message_id, approvals_message(items),
             reply_markup=approvals_keyboard(items) or {"inline_keyboard": []})
    except Exception as e:
        answer_cb(cb_id, "erro")
        try:
            edit(chat_id, message_id, f"Erro: {e}")
        except Exception:
            pass

def check_proactive_alerts():
    """Check for new CRIT incidents and alert once per incident (by id)."""
    if not hasattr(check_proactive_alerts, "_seen"):
        check_proactive_alerts._seen = set()
    try:
        r = requests.get(f"{UI_BASE}/api/incidents", timeout=8).json()
    except Exception:
        return
    if not isinstance(r, list):
        return
    crits = [i for i in r if i.get("status") == "open" and i.get("severity") == "crit"]
    new_crits = [i for i in crits if i.get("id") not in check_proactive_alerts._seen]
    if new_crits:
        lines = [f"🚨 *ALERTA CRÍTICO* — {nowz()}", ""]
        for i in new_crits:
            check_proactive_alerts._seen.add(i.get("id"))
            lines.append(f"🔴 {i.get('title','')} [{i.get('source','')}]")
        lines.append(f"\nResolver: {UI_BASE}/ops")
        try:
            send("\n".join(lines))
        except Exception:
            pass
    # Clean up resolved ids from seen set
    open_ids = {i.get("id") for i in r if i.get("status") == "open"}
    check_proactive_alerts._seen &= open_ids


def main():
    offset = 0
    _tick = 0
    send(f"[{nowz()}] AI-OS bot online. /status /approvals /cluster /alertas")
    while True:
        try:
            r = requests.get(f"{API_BASE}/getUpdates",
                             params={"timeout": 30, "offset": offset}, timeout=40)
            r.raise_for_status()
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd and "text" in upd["message"]:
                    chat_id = str(upd["message"]["chat"]["id"])
                    if TG_CHAT and str(TG_CHAT) != chat_id:
                        continue
                    handle_command(upd["message"]["text"])
                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    if TG_CHAT and str(TG_CHAT) != chat_id:
                        continue
                    handle_callback(cb)
            _tick += 1
            if _tick % 20 == 0:   # every ~10 min (20 × 30s long-poll)
                check_proactive_alerts()
        except Exception as e:
            insert_event("warn", "telegram_bot_error", str(e))
            time.sleep(3)

if __name__ == "__main__":
    main()
