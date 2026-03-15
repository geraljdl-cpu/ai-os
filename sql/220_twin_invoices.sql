-- H — Finance Automation MVP: twin_invoices table
-- Normaliza o estado financeiro que antes estava em twin_entities.metadata

CREATE TABLE IF NOT EXISTS public.twin_invoices (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   BIGINT REFERENCES public.twin_entities(id),
    case_id     BIGINT REFERENCES public.twin_cases(id),
    approval_id BIGINT,
    number      TEXT UNIQUE,                        -- AIOS-2026-0001
    status      TEXT NOT NULL DEFAULT 'issued',     -- issued | paid | overdue | cancelled
    amount      NUMERIC(12,2) NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    client      TEXT,
    due_date    DATE,                               -- issued_at + 30 days
    paid_at     TIMESTAMPTZ,
    pdf_path    TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS twin_invoices_entity ON public.twin_invoices(entity_id);
CREATE INDEX IF NOT EXISTS twin_invoices_status ON public.twin_invoices(status);
CREATE INDEX IF NOT EXISTS twin_invoices_due    ON public.twin_invoices(due_date) WHERE status = 'issued';
