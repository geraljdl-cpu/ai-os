-- Validação Enhanced: ajuste de dias, extras cliente, despesas MB WAY
-- 2026-03-20

-- Novas colunas em event_timesheets
ALTER TABLE public.event_timesheets
    ADD COLUMN IF NOT EXISTS adjusted_days          NUMERIC(4,1),
    ADD COLUMN IF NOT EXISTS adjusted_invoice_net   NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS adjusted_invoice_vat   NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS adjusted_invoice_total NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS client_extras          JSONB DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS approved_by            TEXT;

-- adjusted_*: NULL = sem ajuste (usar invoice_*)
-- client_extras: [{description, amount}] adicionados pelo cliente
-- approved_by: IP ou identificador do cliente

-- Despesas pagas pelo colaborador para reembolso MB WAY pelo cliente
CREATE TABLE IF NOT EXISTS public.timesheet_expenses (
    id                   SERIAL PRIMARY KEY,
    timesheet_id         INT  NOT NULL REFERENCES public.event_timesheets(id) ON DELETE CASCADE,
    worker_id            TEXT NOT NULL,
    worker_name          TEXT NOT NULL,
    worker_phone_mbway   TEXT NOT NULL,
    client_id            TEXT,
    receipt_image_url    TEXT,          -- path relativo em runtime/expenses/
    receipt_name         TEXT,          -- nome no recibo (texto livre)
    receipt_nif_name     TEXT,          -- NIF/nome da entidade no recibo
    amount               NUMERIC(10,2) NOT NULL,
    expense_type         TEXT NOT NULL DEFAULT 'other', -- meal|material|transport|other
    notes                TEXT,
    status               TEXT NOT NULL DEFAULT 'pending_client_review',
    -- pending_client_review | approved_client | rejected_client | reimbursed_mbway
    approved_by          TEXT,
    approved_at          TIMESTAMPTZ,
    rejected_reason      TEXT,
    reimbursed_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_te_expenses_timesheet
    ON public.timesheet_expenses(timesheet_id);

CREATE INDEX IF NOT EXISTS idx_te_expenses_status
    ON public.timesheet_expenses(status);
