-- Sprint L — Reconciliação Bancária
-- Movimentos bancários importados via CSV
CREATE TABLE IF NOT EXISTS public.bank_transactions (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    amount          NUMERIC(12,2) NOT NULL,
    description     TEXT,
    reference       TEXT,
    nif             TEXT,
    status          TEXT NOT NULL DEFAULT 'unmatched'
                        CHECK (status IN ('unmatched','matched','ignored')),
    matched_invoice_id BIGINT REFERENCES public.twin_invoices(id) ON DELETE SET NULL,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reconciliações efectuadas (auto ou manual)
CREATE TABLE IF NOT EXISTS public.bank_reconciliation (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  BIGINT NOT NULL REFERENCES public.bank_transactions(id) ON DELETE CASCADE,
    invoice_id      BIGINT NOT NULL REFERENCES public.twin_invoices(id) ON DELETE CASCADE,
    match_type      TEXT NOT NULL DEFAULT 'manual'
                        CHECK (match_type IN ('auto','manual')),
    confidence      INT NOT NULL DEFAULT 100 CHECK (confidence BETWEEN 0 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bank_tx_status ON public.bank_transactions(status);
CREATE INDEX IF NOT EXISTS idx_bank_tx_date   ON public.bank_transactions(date DESC);
CREATE INDEX IF NOT EXISTS idx_bank_tx_nif    ON public.bank_transactions(nif);
CREATE INDEX IF NOT EXISTS idx_bank_tx_amount ON public.bank_transactions(amount);
CREATE INDEX IF NOT EXISTS idx_bank_recon_tx  ON public.bank_reconciliation(transaction_id);
CREATE INDEX IF NOT EXISTS idx_bank_recon_inv ON public.bank_reconciliation(invoice_id);
