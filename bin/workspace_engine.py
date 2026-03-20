#!/usr/bin/env python3
"""
workspace_engine.py — Bridge CLI para chat com agentes IA no Workspace
Agentes: sonnet (claude-sonnet-4-6) | haiku (claude-haiku-4-5-20251001) | ollama (qwen2.5-coder:14b)

Uso:
  python3 bin/workspace_engine.py new_session <title> <agent>
  python3 bin/workspace_engine.py list_sessions
  python3 bin/workspace_engine.py get_session <session_id>
  python3 bin/workspace_engine.py delete_session <session_id>
  python3 bin/workspace_engine.py chat <session_id> <agent> <message>
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys

import requests
import sqlalchemy as sa

# ── Env ───────────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()

DATABASE_URL  = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://localhost:11434")

MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
    "ollama": "qwen2.5-coder:14b",
}

SYSTEM_PROMPT = """\
És o assistente de IA do AI-OS, o sistema operativo inteligente de João Diogo Lopes.
Respondes em português europeu. Conheces o contexto do projecto AI-OS:
cluster distribuído de 6 nodes, gestão de trabalhadores, seguros, faturas, radar de negócio.
Sê directo, útil e conciso. Para código, usa blocos markdown.
"""

# ── DB ────────────────────────────────────────────────────────────────────────
def _engine():
    return sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)


def _conn():
    return _engine().connect()


# ── Sessões ───────────────────────────────────────────────────────────────────
def new_session(title: str, agent: str) -> dict:
    agent = agent if agent in MODELS else "sonnet"
    with _conn() as c:
        row = c.execute(sa.text(
            "INSERT INTO workspace_sessions(title, agent) VALUES(:t,:a) RETURNING id"
        ), {"t": title, "a": agent}).mappings().first()
        c.commit()
    return {"session_id": row["id"]}


def list_sessions() -> dict:
    with _conn() as c:
        rows = c.execute(sa.text("""
            SELECT s.id, s.title, s.agent, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count,
                   MAX(m.created_at) AS last_message_at
            FROM workspace_sessions s
            LEFT JOIN workspace_messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY COALESCE(MAX(m.created_at), s.created_at) DESC
            LIMIT 50
        """)).mappings().all()
    return {"sessions": [dict(r) for r in rows]}


def get_session(session_id: int) -> dict:
    with _conn() as c:
        sess = c.execute(sa.text(
            "SELECT id, title, agent, created_at, updated_at FROM workspace_sessions WHERE id=:id"
        ), {"id": session_id}).mappings().first()
        if not sess:
            return {"error": f"Sessão {session_id} não encontrada"}
        msgs = c.execute(sa.text("""
            SELECT id, role, content, model, created_at
            FROM workspace_messages WHERE session_id=:id ORDER BY created_at
        """), {"id": session_id}).mappings().all()
    return {"session": dict(sess), "messages": [dict(m) for m in msgs]}


def delete_session(session_id: int) -> dict:
    with _conn() as c:
        c.execute(sa.text("DELETE FROM workspace_sessions WHERE id=:id"), {"id": session_id})
        c.commit()
    return {"ok": True}


# ── Chat ──────────────────────────────────────────────────────────────────────
def _call_anthropic(model_id: str, history: list, user_msg: str) -> str:
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não definida em /etc/aios.env")
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_msg})
    resp = client.messages.create(
        model=model_id,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return resp.content[0].text


def _call_ollama(model_id: str, history: list, user_msg: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_msg})
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={"model": model_id, "messages": messages, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def chat(session_id: int, agent: str, user_msg: str) -> dict:
    agent = agent if agent in MODELS else "sonnet"
    model_id = MODELS[agent]

    # Load history (last 20 messages)
    with _conn() as c:
        sess = c.execute(sa.text(
            "SELECT id FROM workspace_sessions WHERE id=:id"
        ), {"id": session_id}).mappings().first()
        if not sess:
            return {"error": f"Sessão {session_id} não encontrada"}

        history = c.execute(sa.text("""
            SELECT role, content FROM workspace_messages
            WHERE session_id=:id AND role IN ('user','assistant')
            ORDER BY created_at DESC LIMIT 20
        """), {"id": session_id}).mappings().all()
        history = list(reversed(history))

    # Call AI
    if agent == "ollama":
        reply = _call_ollama(model_id, history, user_msg)
    else:
        reply = _call_anthropic(model_id, history, user_msg)

    # Persist user + assistant messages
    with _conn() as c:
        c.execute(sa.text("""
            INSERT INTO workspace_messages(session_id, role, content, model)
            VALUES(:sid, 'user', :content, :model)
        """), {"sid": session_id, "content": user_msg, "model": model_id})
        c.execute(sa.text("""
            INSERT INTO workspace_messages(session_id, role, content, model)
            VALUES(:sid, 'assistant', :content, :model)
        """), {"sid": session_id, "content": reply, "model": model_id})
        c.execute(sa.text(
            "UPDATE workspace_sessions SET updated_at=now() WHERE id=:id"
        ), {"id": session_id})
        c.commit()

    return {"role": "assistant", "content": reply, "model": model_id}


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "Sem comando"}))
        sys.exit(1)

    cmd = args[0]
    try:
        if cmd == "new_session":
            title = args[1] if len(args) > 1 else "Nova sessão"
            agent = args[2] if len(args) > 2 else "sonnet"
            print(json.dumps(new_session(title, agent), default=str))

        elif cmd == "list_sessions":
            print(json.dumps(list_sessions(), default=str))

        elif cmd == "get_session":
            print(json.dumps(get_session(int(args[1])), default=str))

        elif cmd == "delete_session":
            print(json.dumps(delete_session(int(args[1])), default=str))

        elif cmd == "chat":
            session_id = int(args[1])
            agent = args[2]
            message = " ".join(args[3:]) if len(args) > 3 else ""
            if not message:
                print(json.dumps({"error": "Mensagem vazia"}))
                sys.exit(1)
            print(json.dumps(chat(session_id, agent, message), default=str))

        else:
            print(json.dumps({"error": f"Comando desconhecido: {cmd}"}))
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
