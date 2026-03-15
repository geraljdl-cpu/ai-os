-- 340_ideas_decisions.sql
-- Sistema de Ideias (idea_threads, idea_messages, idea_reviews)
-- + Fila de decisões (decision_queue)
-- Seguro correr múltiplas vezes (IF NOT EXISTS)

-- Threads de ideias (cada ideia é um thread)
CREATE TABLE IF NOT EXISTS public.idea_threads (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | telegram | joao_agent
    status      TEXT NOT NULL DEFAULT 'open',     -- open | analysing | project | archived
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idea_threads_status ON public.idea_threads(status, created_at DESC);

-- Mensagens de cada thread (utilizador + IA)
CREATE TABLE IF NOT EXISTS public.idea_messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT NOT NULL REFERENCES public.idea_threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'user',     -- user | assistant | system
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idea_messages_thread ON public.idea_messages(thread_id, created_at);

-- Reviews do Conselho IA (1 por agente por thread)
CREATE TABLE IF NOT EXISTS public.idea_reviews (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT NOT NULL REFERENCES public.idea_threads(id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,                    -- Strategist | Engineering | Operations | Finance | System
    score       NUMERIC(4,1),                     -- 0–10
    summary     TEXT,
    risks       TEXT,
    next_steps  TEXT,
    raw         JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idea_reviews_thread ON public.idea_reviews(thread_id);
CREATE UNIQUE INDEX IF NOT EXISTS idea_reviews_unique ON public.idea_reviews(thread_id, agent);

-- Fila de decisões
CREATE TABLE IF NOT EXISTS public.decision_queue (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'manual',   -- manual | agent_suggestion | idea | tender
    ref_id      TEXT,                             -- ID externo (suggestion_id, idea_id, etc.)
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | cancelled
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS decision_queue_status ON public.decision_queue(status, created_at DESC);
