-- Radar: raw items from each source (never deleted, immutable record of what was fetched)
CREATE TABLE IF NOT EXISTS public.radar_raw_items (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT        NOT NULL,           -- 'ted' | 'base' | 'dr'
    external_id TEXT        NOT NULL,           -- pub_num / anuncio_id / dre_id
    payload     JSONB       NOT NULL,           -- raw API response (kept forever)
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hash        TEXT,                           -- MD5(payload) for change detection
    run_id      BIGINT,                         -- FK to radar_runs (set on insert)
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_radar_raw_source     ON public.radar_raw_items(source);
CREATE INDEX IF NOT EXISTS idx_radar_raw_run        ON public.radar_raw_items(run_id);
CREATE INDEX IF NOT EXISTS idx_radar_raw_fetched    ON public.radar_raw_items(fetched_at DESC);
