-- BLOCO Prompt Inbox — pipeline automático Claude
-- Cria tabela agent_inbox se não existir (pode já existir em produção)

CREATE TABLE IF NOT EXISTS public.agent_inbox (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL DEFAULT 'api',
    target      TEXT NOT NULL DEFAULT 'claude',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|done|error
    sender      TEXT,
    result      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index para polling eficiente de itens pendentes
CREATE INDEX IF NOT EXISTS idx_agent_inbox_pending
    ON public.agent_inbox(status, created_at)
    WHERE status IN ('pending', 'sent');
