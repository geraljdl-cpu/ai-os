-- 310_agent_suggestions.sql
-- Chief of Staff suggestions, alerts and briefings

CREATE TABLE IF NOT EXISTS public.agent_suggestions (
  id         BIGSERIAL PRIMARY KEY,
  kind       TEXT NOT NULL,              -- briefing | alert | task_suggestion
  title      TEXT NOT NULL,
  details    TEXT,
  ref_kind   TEXT,                       -- idea | decision | obligation | task | tender | payout
  ref_id     TEXT,
  score      INT NOT NULL DEFAULT 0,
  is_read    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_suggestions_kind
  ON public.agent_suggestions(kind);

CREATE INDEX IF NOT EXISTS idx_agent_suggestions_created_at
  ON public.agent_suggestions(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_suggestions_ref
  ON public.agent_suggestions(ref_kind, ref_id);

CREATE INDEX IF NOT EXISTS idx_agent_suggestions_unread
  ON public.agent_suggestions(is_read, created_at DESC);
