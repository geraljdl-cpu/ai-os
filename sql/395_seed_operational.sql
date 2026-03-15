-- Seed operacional mínima — parceiros e entidades base
-- Sprint overnight EXTRA 2

-- Parceiros técnicos
INSERT INTO public.companies (name, nif, activity, status, notes)
VALUES
  ('Reciclagem Norte, Lda',   '502000001', 'Reciclagem de metais',        'ativo', 'Parceiro reciclagem cobre'),
  ('Logística Central, Lda',  '503000001', 'Transporte e logística',       'ativo', 'Transporte matéria-prima'),
  ('Manutenção Industrial SA', '504000001', 'Manutenção equipamentos',      'ativo', 'Manutenção granulador')
ON CONFLICT DO NOTHING;

-- Entidade principal da empresa no twin (se não existir)
INSERT INTO public.twin_entities (tenant_id, type, name, status, metadata)
SELECT 'jdl', 'company', c.name, 'active',
       json_build_object('nif', c.nif, 'company_id', c.id, 'activity', c.activity)::jsonb
FROM public.companies c
WHERE NOT EXISTS (
  SELECT 1 FROM public.twin_entities te
  WHERE te.type = 'company' AND te.metadata->>'nif' = c.nif
);

-- Ligar companies ao twin_entity_id
UPDATE public.companies c
SET entity_id = te.id
FROM public.twin_entities te
WHERE te.type = 'company'
  AND te.metadata->>'nif' = c.nif
  AND c.entity_id IS NULL;

-- Pessoa adicional (técnico)
INSERT INTO public.persons (name, nif, role, company_id, status)
VALUES ('Técnico Principal', '100000002', 'tecnico', 1, 'ativo')
ON CONFLICT DO NOTHING;

-- Seed: notas de conhecimento na tabela agent_suggestions para o knowledge base
-- (as decisões são adicionadas via knowledge.py ao Qdrant)
