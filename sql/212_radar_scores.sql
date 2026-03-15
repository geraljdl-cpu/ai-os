-- Radar: scoring results per normalized item
CREATE TABLE IF NOT EXISTS public.radar_scores (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT        NOT NULL,
    external_id    TEXT        NOT NULL,
    normalized_id  BIGINT      REFERENCES public.radar_normalized(id),
    group_name     TEXT,                        -- 'reciclagem_cabos' | etc
    score          INT         NOT NULL,
    score_reasons  JSONB       NOT NULL DEFAULT '[]',
    priority       TEXT,                        -- 'high' | 'medium' | 'low'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id         BIGINT,
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_radar_scores_source   ON public.radar_scores(source);
CREATE INDEX IF NOT EXISTS idx_radar_scores_score    ON public.radar_scores(score DESC);
CREATE INDEX IF NOT EXISTS idx_radar_scores_priority ON public.radar_scores(priority);
CREATE INDEX IF NOT EXISTS idx_radar_scores_run      ON public.radar_scores(run_id);
