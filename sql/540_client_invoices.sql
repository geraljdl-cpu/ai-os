-- BLOCO DINHEIRO: tabelas de faturação estruturada para clientes
-- Separadas de twin_invoices (genérica); focadas no ciclo timesheet → fatura → pago

CREATE TABLE IF NOT EXISTS public.client_invoices (
    id              BIGSERIAL     PRIMARY KEY,
    client_id       BIGINT        NOT NULL REFERENCES public.clients(id),
    timesheet_id    BIGINT        REFERENCES public.event_timesheets(id),
    invoice_number  TEXT,
    status          TEXT          NOT NULL DEFAULT 'invoice_draft',
    -- invoice_draft | invoiced | partially_paid | paid | cancelled
    issue_date      DATE,
    due_date        DATE,
    subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
    tax_total       NUMERIC(12,2) NOT NULL DEFAULT 0,
    total           NUMERIC(12,2) NOT NULL DEFAULT 0,
    pdf_path        TEXT,
    sent_at         TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.client_invoice_lines (
    id          BIGSERIAL     PRIMARY KEY,
    invoice_id  BIGINT        NOT NULL REFERENCES public.client_invoices(id) ON DELETE CASCADE,
    line_type   TEXT          NOT NULL,          -- service | extra | admin_fee
    description TEXT          NOT NULL,
    qty         NUMERIC(12,2) NOT NULL DEFAULT 1,
    unit_price  NUMERIC(12,2) NOT NULL DEFAULT 0,
    amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    meta_json   JSONB         NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS public.payments_received (
    id          BIGSERIAL     PRIMARY KEY,
    invoice_id  BIGINT        NOT NULL REFERENCES public.client_invoices(id) ON DELETE CASCADE,
    amount      NUMERIC(12,2) NOT NULL,
    method      TEXT          NOT NULL,          -- bank_transfer | mbway | check | other
    paid_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    reference   TEXT,
    notes       TEXT
);

-- Link timesheet → client_invoice (para lookup rápido)
ALTER TABLE public.event_timesheets
    ADD COLUMN IF NOT EXISTS client_invoice_id BIGINT
        REFERENCES public.client_invoices(id);

-- Índices
CREATE INDEX IF NOT EXISTS idx_ci_client   ON public.client_invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_ci_ts       ON public.client_invoices(timesheet_id);
CREATE INDEX IF NOT EXISTS idx_ci_status   ON public.client_invoices(status);
CREATE INDEX IF NOT EXISTS idx_cil_inv     ON public.client_invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pr_inv      ON public.payments_received(invoice_id);
