-- BLOCO VALIDAÇÃO ENHANCED — hardening do fluxo de validação
-- Não altera colunas existentes; retrocompatível com registos de produção.

ALTER TABLE public.event_timesheets
    ADD COLUMN IF NOT EXISTS token_expires_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS token_used_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rejection_reason    TEXT,
    ADD COLUMN IF NOT EXISTS needs_revalidation  BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS revalidation_reason TEXT;

-- Backfill: registos submitted ainda pendentes → expiram em 30 dias a contar da criação
UPDATE public.event_timesheets
SET token_expires_at = created_at + INTERVAL '30 days'
WHERE token_expires_at IS NULL
  AND validation_token IS NOT NULL
  AND status = 'submitted';

-- Backfill: registos já processados → token expirou no momento da validação
UPDATE public.event_timesheets
SET token_expires_at = COALESCE(validated_at, created_at) + INTERVAL '1 second'
WHERE token_expires_at IS NULL
  AND validation_token IS NOT NULL;

-- Index para queries de expiração eficientes
CREATE INDEX IF NOT EXISTS idx_et_token_expires
    ON public.event_timesheets(token_expires_at)
    WHERE status = 'submitted';
