-- Council Reviews — generic multi-agent analysis table
-- Sprint: AI Council

CREATE TABLE IF NOT EXISTS public.council_reviews (
  id           SERIAL PRIMARY KEY,
  topic        TEXT NOT NULL,
  topic_kind   TEXT NOT NULL DEFAULT 'general', -- idea | decision | project | architecture | problem | general
  ref_id       TEXT,                            -- optional ref to source entity
  agent        TEXT NOT NULL,                   -- strategist | engineering | operations | finance | system
  analysis     TEXT,
  risks        TEXT,
  opportunity  TEXT,
  score        INT,
  recommendation TEXT,
  raw          TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS council_reviews_kind_idx ON public.council_reviews(topic_kind, created_at DESC);
CREATE INDEX IF NOT EXISTS council_reviews_created_idx ON public.council_reviews(created_at DESC);
