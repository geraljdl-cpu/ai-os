-- Twin Core Migration — AI-OS
-- Compatível com schema existente (não toca em tasks, approvals, events existentes)

-- Adicionar entity_id e data à tabela events existente
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS entity_id bigint;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS data jsonb NOT NULL DEFAULT '{}';

-- 1. Entidades (o "grafo" da empresa)
CREATE TABLE IF NOT EXISTS public.twin_entities (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT 'jdl',
    type        text NOT NULL,
    name        text NOT NULL,
    status      text NOT NULL DEFAULT 'active',
    metadata    jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twin_entities_tenant_type ON public.twin_entities(tenant_id, type);
CREATE INDEX IF NOT EXISTS twin_entities_status ON public.twin_entities(status);

-- 2. Relações entre entidades
CREATE TABLE IF NOT EXISTS public.twin_relations (
    id              bigserial PRIMARY KEY,
    from_entity_id  bigint NOT NULL REFERENCES public.twin_entities(id) ON DELETE CASCADE,
    to_entity_id    bigint NOT NULL REFERENCES public.twin_entities(id) ON DELETE CASCADE,
    rel_type        text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twin_relations_from ON public.twin_relations(from_entity_id);
CREATE INDEX IF NOT EXISTS twin_relations_to ON public.twin_relations(to_entity_id);

-- 3. Workflows (definições de processos)
CREATE TABLE IF NOT EXISTS public.twin_workflows (
    key         text PRIMARY KEY,
    name        text NOT NULL,
    version     int NOT NULL DEFAULT 1,
    definition  jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 4. Cases (instâncias de processo)
CREATE TABLE IF NOT EXISTS public.twin_cases (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL DEFAULT 'jdl',
    workflow_key    text NOT NULL REFERENCES public.twin_workflows(key),
    entity_id       bigint REFERENCES public.twin_entities(id),
    status          text NOT NULL DEFAULT 'open',
    sla_due_at      timestamptz,
    data            jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twin_cases_tenant ON public.twin_cases(tenant_id, status);
CREATE INDEX IF NOT EXISTS twin_cases_entity ON public.twin_cases(entity_id);

-- 5. Tasks do Twin (separado do public.tasks do autopilot)
CREATE TABLE IF NOT EXISTS public.twin_tasks (
    id          bigserial PRIMARY KEY,
    case_id     bigint NOT NULL REFERENCES public.twin_cases(id) ON DELETE CASCADE,
    title       text NOT NULL,
    type        text NOT NULL DEFAULT 'auto',
    status      text NOT NULL DEFAULT 'pending',
    assignee    text,
    due_at      timestamptz,
    payload     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twin_tasks_case ON public.twin_tasks(case_id, status);

-- 6. Approvals do Twin (separado do public.approvals do autopilot)
CREATE TABLE IF NOT EXISTS public.twin_approvals (
    id              bigserial PRIMARY KEY,
    case_id         bigint REFERENCES public.twin_cases(id),
    action          text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    requested_by    text NOT NULL DEFAULT 'system',
    approved_by     text,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    approved_at     timestamptz,
    context         jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS twin_approvals_status ON public.twin_approvals(status);

-- 7. Document templates
CREATE TABLE IF NOT EXISTS public.twin_document_templates (
    key              text PRIMARY KEY,
    version          int NOT NULL DEFAULT 1,
    format           text NOT NULL DEFAULT 'pdf',
    content          text,
    variables_schema jsonb NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- 8. Documents
CREATE TABLE IF NOT EXISTS public.twin_documents (
    id           bigserial PRIMARY KEY,
    tenant_id    text NOT NULL DEFAULT 'jdl',
    entity_id    bigint REFERENCES public.twin_entities(id),
    template_key text REFERENCES public.twin_document_templates(key),
    status       text NOT NULL DEFAULT 'draft',
    storage_uri  text,
    hash         text,
    metadata     jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS twin_documents_entity ON public.twin_documents(entity_id);

-- 9. Document requests
CREATE TABLE IF NOT EXISTS public.twin_document_requests (
    id            bigserial PRIMARY KEY,
    entity_id     bigint REFERENCES public.twin_entities(id),
    document_type text NOT NULL,
    status        text NOT NULL DEFAULT 'pending',
    due_at        timestamptz,
    channel       text DEFAULT 'telegram',
    link          text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Workflow inicial: cable_batch (fábrica de cabos)
INSERT INTO public.twin_workflows (key, name, version, definition) VALUES (
    'cable_batch',
    'Processamento de Lote de Cabos',
    1,
    '{
        "steps": ["agendado","chegou","em_processamento","separacao","concluido","pronto_levantar","faturado"],
        "initial": "agendado",
        "final": ["faturado"],
        "transitions": {
            "agendado": ["chegou"],
            "chegou": ["em_processamento"],
            "em_processamento": ["separacao"],
            "separacao": ["concluido"],
            "concluido": ["pronto_levantar"],
            "pronto_levantar": ["faturado"]
        }
    }'
) ON CONFLICT (key) DO NOTHING;
