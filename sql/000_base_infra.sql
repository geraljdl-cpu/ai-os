-- 000_base_infra.sql
-- Tabelas de infraestrutura: telemetry, workers, worker_jobs, finance_payouts
-- Aplicar antes de twin_core e demais migrations

-- Telemetria de nós (NOC sparklines)
CREATE TABLE IF NOT EXISTS public.telemetry (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hostname        TEXT NOT NULL,
    cpu_pct         NUMERIC(5,2),
    mem_used_mb     INT,
    mem_total_mb    INT,
    disk_used_gb    NUMERIC(10,2),
    disk_total_gb   NUMERIC(10,2),
    load1           NUMERIC(6,2),
    backlog_pending INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS telemetry_ts_host ON public.telemetry(hostname, ts DESC);

-- Workers registados (cluster nodes)
CREATE TABLE IF NOT EXISTS public.workers (
    id          TEXT PRIMARY KEY,
    hostname    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'worker',
    status      TEXT NOT NULL DEFAULT 'online',
    token       TEXT,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata    JSONB NOT NULL DEFAULT '{}'
);

-- Fila de jobs para workers
CREATE TABLE IF NOT EXISTS public.worker_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    ts_created          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ts_assigned         TIMESTAMPTZ,
    ts_done             TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'queued',
    kind                TEXT NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}',
    result              JSONB,
    target_worker_id    TEXT REFERENCES public.workers(id) ON DELETE SET NULL,
    assigned_worker_id  TEXT REFERENCES public.workers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS worker_jobs_status ON public.worker_jobs(status, ts_created);

-- Pagamentos a trabalhadores
CREATE TABLE IF NOT EXISTS public.finance_payouts (
    id          BIGSERIAL PRIMARY KEY,
    worker_id   TEXT NOT NULL,
    week_start  DATE NOT NULL,
    total_hours NUMERIC(8,2) NOT NULL DEFAULT 0,
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    paid_at     TIMESTAMPTZ,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (worker_id, week_start)
);
CREATE INDEX IF NOT EXISTS finance_payouts_worker ON public.finance_payouts(worker_id, week_start);
CREATE INDEX IF NOT EXISTS finance_payouts_status ON public.finance_payouts(status);
