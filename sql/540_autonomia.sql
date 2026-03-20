-- 540_autonomia.sql — Extensões para execução semi-autónoma controlada
-- Aplicar: cat sql/540_autonomia.sql | docker exec -i postgres psql -U aios_user -d aios

-- ── worker_jobs: coluna para registo de aprovação ─────────────────────────────
ALTER TABLE public.worker_jobs
    ADD COLUMN IF NOT EXISTS approved_by TEXT;

-- ── autonomia_guardrails: padrões que bloqueiam execução automática ───────────
CREATE TABLE IF NOT EXISTS public.autonomia_guardrails (
    id          SERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,                        -- 'shell'|'automation'|'*'
    pattern     TEXT NOT NULL,                        -- substring a detectar no payload (lower)
    action      TEXT NOT NULL DEFAULT 'block',        -- 'block'|'warn'
    description TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seeds: padrões destrutivos
INSERT INTO public.autonomia_guardrails (kind, pattern, action, description) VALUES
    ('shell',      'rm -rf',            'block', 'remoção recursiva forçada'),
    ('shell',      'drop table',        'block', 'drop table'),
    ('shell',      'truncate',          'block', 'truncate table'),
    ('shell',      'delete from',       'block', 'delete sem filtro'),
    ('shell',      'mkfs',              'block', 'formatar partição'),
    ('shell',      'shutdown',          'block', 'shutdown sistema'),
    ('automation', 'rm -rf',            'block', 'remoção recursiva forçada'),
    ('automation', 'drop table',        'block', 'drop table'),
    ('*',          'requires_approval', 'block', 'flag manual no payload')
ON CONFLICT DO NOTHING;

-- ── agent_inbox: flag para itens que requerem revisão humana ─────────────────
ALTER TABLE public.agent_inbox
    ADD COLUMN IF NOT EXISTS requires_review BOOLEAN NOT NULL DEFAULT false;
