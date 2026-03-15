-- 370_documents.sql
-- Document Vault: documents, document_requirements, document_requests, document_actions

-- ── Core document registry ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.documents (
  id            SERIAL PRIMARY KEY,
  owner_type    TEXT NOT NULL,          -- person | company | vehicle | project | case
  owner_id      INT  NOT NULL,
  doc_type      TEXT NOT NULL,          -- certidao_permanente | nao_divida_at | seguro_viatura | …
  title         TEXT NOT NULL,
  issuer        TEXT,
  file_path     TEXT,
  issue_date    DATE,
  expiry_date   DATE,
  status        TEXT NOT NULL DEFAULT 'valid',   -- valid | expiring | expired | missing
  sensitivity   TEXT NOT NULL DEFAULT 'normal',  -- normal | restricted | critical
  source        TEXT NOT NULL DEFAULT 'manual',  -- manual | upload | portal | generated
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS documents_owner     ON public.documents(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS documents_type      ON public.documents(doc_type);
CREATE INDEX IF NOT EXISTS documents_expiry    ON public.documents(expiry_date) WHERE expiry_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS documents_status    ON public.documents(status);

-- ── Document requirements by context ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.document_requirements (
  id            SERIAL PRIMARY KEY,
  context_type  TEXT NOT NULL,   -- tender | vehicle | tax | supplier | company | person
  target_type   TEXT NOT NULL,   -- owner_type que deve ter este doc
  doc_type      TEXT NOT NULL,
  required      BOOLEAN NOT NULL DEFAULT TRUE,
  max_age_days  INT,             -- NULL = sem limite de validade
  notes         TEXT,
  UNIQUE(context_type, target_type, doc_type)
);

-- Seed: requisitos standard
INSERT INTO public.document_requirements (context_type, target_type, doc_type, required, max_age_days, notes)
VALUES
  -- Concurso público (tender): empresa precisa de certidões
  ('tender',  'company', 'certidao_permanente', true,  365,  'Certidão Permanente do Registo Comercial'),
  ('tender',  'company', 'nao_divida_at',       true,  90,   'Declaração de não dívida AT'),
  ('tender',  'company', 'nao_divida_ss',       true,  90,   'Declaração de não dívida SS'),
  ('tender',  'person',  'registo_criminal',    true,  180,  'Registo Criminal do representante'),
  -- Viatura
  ('vehicle', 'vehicle', 'seguro_viatura',      true,  365,  'Seguro obrigatório de responsabilidade civil'),
  ('vehicle', 'vehicle', 'inspecao_viatura',    true,  730,  'Inspeção periódica obrigatória'),
  ('vehicle', 'vehicle', 'iuc',                 true,  365,  'Imposto Único de Circulação'),
  -- Fiscal anual
  ('tax',     'company', 'nao_divida_at',       true,  90,   'AT — renovar antes de concursos'),
  ('tax',     'company', 'nao_divida_ss',       true,  90,   'SS — renovar antes de concursos')
ON CONFLICT (context_type, target_type, doc_type) DO NOTHING;

-- ── Document requests ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.document_requests (
  id             SERIAL PRIMARY KEY,
  requirement_id INT REFERENCES public.document_requirements(id),
  owner_type     TEXT NOT NULL,
  owner_id       INT  NOT NULL,
  doc_type       TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'open',  -- open | drafted | ready_for_approval | done | failed
  process_type   TEXT,           -- contexto que originou (ex: tender_intake)
  linked_case_id INT,
  requested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  due_date       DATE,
  notes          TEXT
);

CREATE INDEX IF NOT EXISTS doc_requests_status ON public.document_requests(status);
CREATE INDEX IF NOT EXISTS doc_requests_owner  ON public.document_requests(owner_type, owner_id);

-- ── Document actions (audit trail) ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.document_actions (
  id          SERIAL PRIMARY KEY,
  document_id INT REFERENCES public.documents(id),
  request_id  INT REFERENCES public.document_requests(id),
  action      TEXT NOT NULL,  -- created | updated | approved | rejected | expired | alerted
  actor       TEXT NOT NULL DEFAULT 'system',  -- system | joao | agent | <username>
  status      TEXT NOT NULL DEFAULT 'done',
  payload     JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS doc_actions_doc ON public.document_actions(document_id);
CREATE INDEX IF NOT EXISTS doc_actions_req ON public.document_actions(request_id);
