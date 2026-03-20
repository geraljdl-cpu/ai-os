-- Workspace: sessões de chat com agentes IA
-- 2026-03-19

CREATE TABLE IF NOT EXISTS workspace_sessions (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'Nova sessão',
    agent       TEXT NOT NULL DEFAULT 'sonnet',  -- sonnet | haiku | ollama
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace_messages (
    id          SERIAL PRIMARY KEY,
    session_id  INT NOT NULL REFERENCES workspace_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content     TEXT NOT NULL,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workspace_messages_session ON workspace_messages(session_id, created_at);
