-- Radar: audit log of each pipeline run
CREATE TABLE IF NOT EXISTS public.radar_runs (
    id               BIGSERIAL PRIMARY KEY,
    source           TEXT        NOT NULL,      -- 'ted' | 'base' | 'dr' | 'all'
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    raw_count        INT         NOT NULL DEFAULT 0,
    normalized_count INT         NOT NULL DEFAULT 0,
    scored_count     INT         NOT NULL DEFAULT 0,
    twin_created     INT         NOT NULL DEFAULT 0,
    twin_updated     INT         NOT NULL DEFAULT 0,
    status           TEXT        NOT NULL DEFAULT 'running',  -- 'running' | 'ok' | 'error'
    error_log        TEXT
);

CREATE INDEX IF NOT EXISTS idx_radar_runs_source  ON public.radar_runs(source);
CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON public.radar_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_runs_status  ON public.radar_runs(status);
