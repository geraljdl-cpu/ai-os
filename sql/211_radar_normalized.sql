-- Radar: normalized schema — common fields across TED / BASE / DR
CREATE TABLE IF NOT EXISTS public.radar_normalized (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT        NOT NULL,          -- 'ted' | 'base' | 'dr'
    external_id  TEXT        NOT NULL,          -- same key as radar_raw_items
    title        TEXT,
    entity_name  TEXT,                          -- adjudicante
    description  TEXT,
    cpv          TEXT,                          -- first CPV code
    deadline     DATE,
    base_value   NUMERIC,                       -- valor base sem IVA
    country      TEXT        NOT NULL DEFAULT 'PT',
    region       TEXT,
    url          TEXT,
    published_at DATE,
    raw_item_id  BIGINT REFERENCES public.radar_raw_items(id),
    run_id       BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_radar_norm_source  ON public.radar_normalized(source);
CREATE INDEX IF NOT EXISTS idx_radar_norm_run     ON public.radar_normalized(run_id);
CREATE INDEX IF NOT EXISTS idx_radar_norm_pub     ON public.radar_normalized(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_norm_cpv     ON public.radar_normalized(cpv);
