-- 470_commercial.sql — Módulo Comercial: pedidos, orçamentos, preços

CREATE TABLE IF NOT EXISTS public.commercial_price_rules (
    id           SERIAL PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    category     TEXT NOT NULL DEFAULT 'geral',
    description  TEXT NOT NULL,
    unit         TEXT NOT NULL DEFAULT 'un',
    unit_price   NUMERIC(12,4) NOT NULL DEFAULT 0,
    vat_rate     NUMERIC(5,2)  NOT NULL DEFAULT 23.0,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_price_rules_cat ON public.commercial_price_rules(category) WHERE active;

CREATE TABLE IF NOT EXISTS public.commercial_requests (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT NOT NULL DEFAULT 'manual',
    customer_name   TEXT,
    company_name    TEXT,
    customer_email  TEXT,
    customer_phone  TEXT,
    event_type      TEXT,
    location        TEXT,
    event_date      DATE,
    start_time      TEXT,
    end_time        TEXT,
    raw_request     TEXT NOT NULL DEFAULT '',
    parsed_json     JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'new',
    assigned_to     TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_commercial_requests_status ON public.commercial_requests(status);
CREATE INDEX IF NOT EXISTS idx_commercial_requests_created ON public.commercial_requests(created_at DESC);

CREATE TABLE IF NOT EXISTS public.commercial_quotes (
    id              SERIAL PRIMARY KEY,
    request_id      INT REFERENCES public.commercial_requests(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quote_number    TEXT NOT NULL UNIQUE,
    title           TEXT,
    line_items      JSONB NOT NULL DEFAULT '[]',
    subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,
    vat_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    total           NUMERIC(12,2) NOT NULL DEFAULT 0,
    assumptions     TEXT,
    exclusions      TEXT,
    validity_days   INT NOT NULL DEFAULT 30,
    status          TEXT NOT NULL DEFAULT 'draft',
    pdf_path        TEXT,
    email_sent_at   TIMESTAMPTZ,
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_commercial_quotes_status ON public.commercial_quotes(status);
CREATE INDEX IF NOT EXISTS idx_commercial_quotes_request ON public.commercial_quotes(request_id);

-- Seed some price rules
INSERT INTO public.commercial_price_rules (code, category, description, unit, unit_price, vat_rate) VALUES
    ('MAN_OBR_DIA',    'mao_de_obra',  'Mão de obra — dia completo (8-12h)', 'dia',  100.00, 23.0),
    ('MAN_OBR_MEIO',   'mao_de_obra',  'Mão de obra — meio dia (4h)',         'dia',   60.00, 23.0),
    ('TRANSP_KM',      'transporte',   'Transporte — por km',                 'km',     0.50, 23.0),
    ('TRANSP_FLAT',    'transporte',   'Transporte — taxa fixa',              'flat',  50.00, 23.0),
    ('EQUIP_BASIC',    'equipamento',  'Equipamento básico — diária',         'dia',   30.00, 23.0),
    ('COORD_HR',       'coordenacao',  'Coordenação técnica — hora',          'hr',    45.00, 23.0)
ON CONFLICT (code) DO NOTHING;
