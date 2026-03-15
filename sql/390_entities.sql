-- 390_entities.sql
-- Pessoas e Empresas — donos reais de documentos, viaturas, contratos

CREATE TABLE IF NOT EXISTS public.companies (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  nif         TEXT UNIQUE,
  nipc        TEXT UNIQUE,
  activity    TEXT,                -- CAE / descrição
  address     TEXT,
  email       TEXT,
  phone       TEXT,
  entity_id   INT,                 -- FK twin_entities (opcional)
  status      TEXT NOT NULL DEFAULT 'active',  -- active | inactive | archived
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.persons (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  nif         TEXT UNIQUE,
  role        TEXT,                -- gerente | trabalhador | fornecedor | sócio
  company_id  INT REFERENCES public.companies(id),
  email       TEXT,
  phone       TEXT,
  entity_id   INT,
  status      TEXT NOT NULL DEFAULT 'active',
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS companies_nif    ON public.companies(nif) WHERE nif IS NOT NULL;
CREATE INDEX IF NOT EXISTS persons_company  ON public.persons(company_id);
CREATE INDEX IF NOT EXISTS persons_nif      ON public.persons(nif) WHERE nif IS NOT NULL;

-- Seed: empresa principal + representante legal
INSERT INTO public.companies (name, nif, activity, status, notes)
VALUES ('Empresa Principal', '500000001', 'Tratamento e valorização de resíduos (CAE 38320)', 'active', 'Empresa operacional principal')
ON CONFLICT (nif) DO NOTHING;

INSERT INTO public.persons (name, nif, role, company_id, status, notes)
VALUES ('João (Gerente)', '100000001', 'gerente', 1, 'active', 'Representante legal e gerente')
ON CONFLICT (nif) DO NOTHING;
