-- Finance MVP: event_timesheets
-- Registo de horas de técnicos por evento

CREATE TABLE IF NOT EXISTS public.event_timesheets (
    id           BIGSERIAL PRIMARY KEY,
    worker_id    TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    event_id     BIGINT,                              -- opcional: linked to twin_entity
    start_time   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_time     TIMESTAMPTZ,
    hours        NUMERIC(6,2),                        -- calculado ao parar
    notes        TEXT,
    status       TEXT NOT NULL DEFAULT 'open',        -- open | submitted | approved | paid
    hourly_rate  NUMERIC(8,2),                        -- valor/hora do worker
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS event_timesheets_worker ON public.event_timesheets(worker_id);
CREATE INDEX IF NOT EXISTS event_timesheets_status ON public.event_timesheets(status);
CREATE INDEX IF NOT EXISTS event_timesheets_event  ON public.event_timesheets(event_id);
