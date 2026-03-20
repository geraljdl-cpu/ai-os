-- 480_insurance.sql — Módulo Seguros
-- Tabelas: insurance_policies, insurance_documents, insurance_alerts, vehicles_insurance_link

CREATE TABLE IF NOT EXISTS public.insurance_policies (
  id               SERIAL PRIMARY KEY,
  entity_type      TEXT NOT NULL DEFAULT 'company',
  -- vehicle, company, person, property, equipment, liability, workers_comp
  entity_ref       TEXT,           -- matrícula, NIF, nome
  insurer_name     TEXT NOT NULL,
  policy_number    TEXT UNIQUE,
  category         TEXT,           -- automovel, multirriscos, rc, acidentes_trabalho, vida, saude
  coverage_summary TEXT,
  start_date       DATE,
  end_date         DATE,
  renewal_date     DATE,
  payment_frequency TEXT DEFAULT 'annual',
  premium_amount   NUMERIC(10,2),
  status           TEXT NOT NULL DEFAULT 'active',  -- active,pending,expired,cancelled
  auto_renew       BOOLEAN NOT NULL DEFAULT TRUE,
  notes            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.insurance_documents (
  id              SERIAL PRIMARY KEY,
  policy_id       INT REFERENCES public.insurance_policies(id),
  doc_type        TEXT NOT NULL DEFAULT 'policy',
  -- policy, receipt, invoice, claim, amendment, green_card, proposal
  file_path       TEXT,
  source_type     TEXT DEFAULT 'manual',  -- upload, email, manual
  extracted_text  TEXT,
  issue_date      DATE,
  due_date        DATE,
  amount          NUMERIC(10,2),
  review_required BOOLEAN NOT NULL DEFAULT FALSE,
  metadata_json   JSONB DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.insurance_alerts (
  id               SERIAL PRIMARY KEY,
  policy_id        INT REFERENCES public.insurance_policies(id),
  alert_type       TEXT NOT NULL,
  -- renewal, payment_due, expired, missing_document, inspection_related
  trigger_date     DATE NOT NULL,
  sent_telegram_at TIMESTAMPTZ,
  sent_email_at    TIMESTAMPTZ,
  resolved_at      TIMESTAMPTZ,
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending, sent, resolved, dismissed
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (policy_id, alert_type, trigger_date)
);

CREATE TABLE IF NOT EXISTS public.vehicles_insurance_link (
  id              SERIAL PRIMARY KEY,
  vehicle_id      INT,
  policy_id       INT REFERENCES public.insurance_policies(id),
  plate           TEXT,
  insurer_name    TEXT,
  inspection_date DATE,
  iuc_date        DATE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ins_pol_status    ON public.insurance_policies(status);
CREATE INDEX IF NOT EXISTS idx_ins_pol_renewal   ON public.insurance_policies(renewal_date);
CREATE INDEX IF NOT EXISTS idx_ins_pol_entity    ON public.insurance_policies(entity_ref);
CREATE INDEX IF NOT EXISTS idx_ins_doc_policy    ON public.insurance_documents(policy_id);
CREATE INDEX IF NOT EXISTS idx_ins_alert_status  ON public.insurance_alerts(status);
CREATE INDEX IF NOT EXISTS idx_ins_alert_trigger ON public.insurance_alerts(trigger_date);
