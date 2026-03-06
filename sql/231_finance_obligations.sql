-- Finance MVP: finance_obligations
-- Calendário fiscal — IVA, SS, AT, IRS, retenções

CREATE TABLE IF NOT EXISTS public.finance_obligations (
    id         BIGSERIAL PRIMARY KEY,
    type       TEXT NOT NULL,              -- iva | ss_empresa | ss_pessoal | irs | retencoes | irc | outro
    entity     TEXT NOT NULL,             -- 'AT', 'Segurança Social', etc.
    label      TEXT NOT NULL,             -- e.g. "IVA Q1 2026"
    due_date   DATE NOT NULL,
    amount     NUMERIC(12,2),             -- NULL = desconhecido ainda
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | cancelled
    source     TEXT,                      -- 'manual' | 'auto' | 'toconline'
    notes      TEXT,
    paid_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS finance_obligations_due    ON public.finance_obligations(due_date);
CREATE INDEX IF NOT EXISTS finance_obligations_status ON public.finance_obligations(status);

-- Seeds: 2026 (referência: hoje 2026-03-06)
-- IVA trimestral (prazo: dia 20 do 2º mês após trimestre)
INSERT INTO public.finance_obligations (type, entity, label, due_date, status, source) VALUES
  ('iva',       'AT',               'IVA Q4 2025',         '2026-01-20', 'pending', 'auto'),
  ('iva',       'AT',               'IVA Q1 2026',         '2026-04-20', 'pending', 'auto'),
  ('iva',       'AT',               'IVA Q2 2026',         '2026-07-20', 'pending', 'auto'),
  ('iva',       'AT',               'IVA Q3 2026',         '2026-10-20', 'pending', 'auto'),
-- SS empresa: mensal, dia 20 do mês seguinte
  ('ss_empresa','Segurança Social', 'SS Empresa Jan 2026', '2026-02-20', 'pending', 'auto'),
  ('ss_empresa','Segurança Social', 'SS Empresa Fev 2026', '2026-03-20', 'pending', 'auto'),
  ('ss_empresa','Segurança Social', 'SS Empresa Mar 2026', '2026-04-20', 'pending', 'auto'),
  ('ss_empresa','Segurança Social', 'SS Empresa Abr 2026', '2026-05-20', 'pending', 'auto'),
  ('ss_empresa','Segurança Social', 'SS Empresa Mai 2026', '2026-06-20', 'pending', 'auto'),
  ('ss_empresa','Segurança Social', 'SS Empresa Jun 2026', '2026-07-20', 'pending', 'auto'),
-- IRS entrega (rendimentos 2025)
  ('irs',       'AT',               'IRS Entrega 2025',    '2026-04-30', 'pending', 'auto'),
-- IRC pagamento especial por conta (para ENI/Lda se aplicável)
  ('irc',       'AT',               'IRC PEC 2026',        '2026-03-31', 'pending', 'auto')
ON CONFLICT DO NOTHING;
