-- BLOCO RH — Gestão de Pessoas, Contratos e Documentos
-- Retrocompatível: não altera tabela persons existente

-- Dados pessoais alargados (1-to-1 com persons)
CREATE TABLE IF NOT EXISTS public.hr_persons_extra (
    person_id           INTEGER PRIMARY KEY REFERENCES public.persons(id) ON DELETE CASCADE,
    iban                TEXT,
    niss                TEXT,
    data_nascimento     DATE,
    morada              TEXT,
    data_admissao       DATE,
    tipo_contrato_atual TEXT,   -- 'trabalho_prazo'|'trabalho_sem_termo'|'recibo_verde'|'nda'|null
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Contratos de trabalho
CREATE TABLE IF NOT EXISTS public.hr_contracts (
    id          SERIAL PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,  -- 'trabalho_prazo'|'trabalho_sem_termo'|'recibo_verde'|'nda'
    data_inicio DATE NOT NULL,
    data_fim    DATE,           -- NULL = sem termo / indeterminado
    notas       TEXT,
    file_path   TEXT,           -- nome do ficheiro em runtime/hr_docs/
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hr_contracts_person ON public.hr_contracts(person_id);
CREATE INDEX IF NOT EXISTS idx_hr_contracts_expiry ON public.hr_contracts(data_fim)
    WHERE data_fim IS NOT NULL;

-- Documentos de RH (PDFs)
CREATE TABLE IF NOT EXISTS public.hr_documents (
    id          SERIAL PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES public.persons(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,  -- 'contrato'|'id'|'iban'|'certificado'|'outro'
    descricao   TEXT,
    file_path   TEXT NOT NULL,  -- nome do ficheiro em runtime/hr_docs/
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hr_documents_person ON public.hr_documents(person_id);
