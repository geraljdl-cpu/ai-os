-- Expand document types with missing entries for person/company/vehicle
-- Add missing columns to documents if needed
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS alert_days INT NOT NULL DEFAULT 30;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS renewed_at DATE;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ;

-- Document type catalog (reference only, not enforced by FK)
CREATE TABLE IF NOT EXISTS public.document_types (
  doc_type    TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  owner_types TEXT[] NOT NULL DEFAULT '{person,company,vehicle}',
  alert_days  INT  NOT NULL DEFAULT 30,
  required    BOOLEAN NOT NULL DEFAULT FALSE,
  notes       TEXT
);

INSERT INTO public.document_types (doc_type, label, owner_types, alert_days, required) VALUES
  ('certidao_permanente',   'Certidão Permanente',         '{person,company}', 90,  true),
  ('registo_criminal',      'Registo Criminal',            '{person}',         60,  true),
  ('nao_divida_at',         'Não Dívida AT',               '{person,company}', 30,  true),
  ('nao_divida_ss',         'Não Dívida SS',               '{person,company}', 30,  true),
  ('contrato',              'Contrato/Anexo',              '{person,company}', 365, false),
  ('seguro_viatura',        'Seguro Viatura',              '{vehicle}',        30,  true),
  ('inspecao_viatura',      'Inspeção Viatura',            '{vehicle}',        30,  true),
  ('iuc',                   'IUC',                         '{vehicle}',        30,  true),
  ('manutencao_viatura',    'Manutenção Viatura',          '{vehicle}',        60,  false),
  ('bilhete_identidade',    'BI / Cartão de Cidadão',      '{person}',         180, true),
  ('titulo_residencia',     'Título de Residência',        '{person}',         90,  true),
  ('alvara',                'Alvará',                      '{company}',        60,  true),
  ('livro_obra',            'Livro de Obra',               '{company,case}',   0,   false),
  ('outro',                 'Outro',                       '{person,company,vehicle}', 30, false)
ON CONFLICT (doc_type) DO UPDATE SET label=EXCLUDED.label, alert_days=EXCLUDED.alert_days;
