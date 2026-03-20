-- BLOCO CASHFLOW: worker_payouts — registo individual por timesheet
-- Complementa finance_payouts (agregação semanal) com granularidade por serviço

CREATE TABLE IF NOT EXISTS public.worker_payouts (
    id              BIGSERIAL     PRIMARY KEY,
    timesheet_id    BIGINT        NOT NULL REFERENCES public.event_timesheets(id),
    worker_id       TEXT,
    client_id       BIGINT        REFERENCES public.clients(id),
    amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
    status          TEXT          NOT NULL DEFAULT 'pending',
    -- pending | approved | paid | cancelled
    due_date        DATE,
    paid_at         TIMESTAMPTZ,
    payment_method  TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wp_timesheet ON public.worker_payouts(timesheet_id);
CREATE INDEX IF NOT EXISTS idx_wp_status    ON public.worker_payouts(status);
CREATE INDEX IF NOT EXISTS idx_wp_worker    ON public.worker_payouts(worker_id);
