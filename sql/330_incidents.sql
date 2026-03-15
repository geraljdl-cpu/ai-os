-- Sprint M — Incidentes e Alarmes
CREATE TABLE IF NOT EXISTS public.incidents (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,            -- workers, tasks, finance, tender, infra, bank
    kind        TEXT NOT NULL,            -- worker_offline, task_blocked, obligation_due, tender_urgent, api_failed, bank_unmatched
    severity    TEXT NOT NULL DEFAULT 'warn'
                    CHECK (severity IN ('info','warn','crit')),
    title       TEXT NOT NULL,
    details     TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','resolved')),
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_incidents_status   ON public.incidents(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON public.incidents(severity) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_incidents_source   ON public.incidents(source, kind);
