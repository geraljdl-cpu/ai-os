-- BLOCO PLANEAMENTO: escalas e alocação de colaboradores por serviço

CREATE TABLE IF NOT EXISTS public.service_jobs (
    id              BIGSERIAL   PRIMARY KEY,
    client_id       BIGINT      NOT NULL REFERENCES public.clients(id),
    title           TEXT        NOT NULL,
    location        TEXT,
    starts_at       TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ NOT NULL,
    needed_workers  INTEGER     NOT NULL DEFAULT 1,
    notes           TEXT,
    status          TEXT        NOT NULL DEFAULT 'planned',
    -- planned | ready | in_progress | completed | cancelled
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.job_assignments (
    id              BIGSERIAL   PRIMARY KEY,
    job_id          BIGINT      NOT NULL REFERENCES public.service_jobs(id) ON DELETE CASCADE,
    worker_id       BIGINT      NOT NULL REFERENCES public.persons(id),
    role            TEXT,
    status          TEXT        NOT NULL DEFAULT 'assigned',
    -- assigned | confirmed | checked_in | completed | cancelled
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sj_client   ON public.service_jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_sj_status   ON public.service_jobs(status);
CREATE INDEX IF NOT EXISTS idx_sj_starts   ON public.service_jobs(starts_at);
CREATE INDEX IF NOT EXISTS idx_ja_job      ON public.job_assignments(job_id);
CREATE INDEX IF NOT EXISTS idx_ja_worker   ON public.job_assignments(worker_id);
CREATE INDEX IF NOT EXISTS idx_ja_status   ON public.job_assignments(status);
