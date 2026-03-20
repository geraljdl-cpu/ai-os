-- BLOCO MARKETPLACE: linha paralela de matching — não altera fluxo Mauro/Ryan/Oneway

CREATE TABLE IF NOT EXISTS public.marketplace_jobs (
    id              BIGSERIAL   PRIMARY KEY,
    client_id       BIGINT      NOT NULL REFERENCES public.clients(id),
    title           TEXT        NOT NULL,
    location        TEXT,
    starts_at       TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ NOT NULL,
    needed_workers  INTEGER     NOT NULL DEFAULT 1,
    role_required   TEXT,
    billing_model   TEXT        NOT NULL DEFAULT 'marketplace_direct',
    status          TEXT        NOT NULL DEFAULT 'open',
    -- open | matched | closed | cancelled
    notes           TEXT,
    service_job_id  BIGINT      REFERENCES public.service_jobs(id),
    -- preenchido quando primeiro worker é selected → ponte para o fluxo normal
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.marketplace_applications (
    id              BIGSERIAL   PRIMARY KEY,
    job_id          BIGINT      NOT NULL REFERENCES public.marketplace_jobs(id) ON DELETE CASCADE,
    worker_id       BIGINT      NOT NULL REFERENCES public.persons(id),
    status          TEXT        NOT NULL DEFAULT 'invited',
    -- invited | accepted | declined | selected | rejected | expired
    response_at     TIMESTAMPTZ,
    source          TEXT        NOT NULL DEFAULT 'whatsapp',
    score           NUMERIC(8,2) NOT NULL DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.marketplace_worker_profiles (
    id                  BIGSERIAL   PRIMARY KEY,
    worker_id           BIGINT      NOT NULL REFERENCES public.persons(id),
    whatsapp_phone      TEXT,
    active              BOOLEAN     NOT NULL DEFAULT true,
    roles_json          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    zones_json          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    rating              NUMERIC(4,2) NOT NULL DEFAULT 0,
    documents_ok        BOOLEAN     NOT NULL DEFAULT false,
    marketplace_enabled BOOLEAN     NOT NULL DEFAULT false,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mj_client   ON public.marketplace_jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_mj_status   ON public.marketplace_jobs(status);
CREATE INDEX IF NOT EXISTS idx_ma_job      ON public.marketplace_applications(job_id);
CREATE INDEX IF NOT EXISTS idx_ma_worker   ON public.marketplace_applications(worker_id);
CREATE INDEX IF NOT EXISTS idx_ma_status   ON public.marketplace_applications(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mwp_worker ON public.marketplace_worker_profiles(worker_id);
