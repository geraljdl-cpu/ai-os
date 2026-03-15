-- 380_vehicles.sql
-- Asset registry: viaturas (preparado para pessoa, empresa, projeto)

CREATE TABLE IF NOT EXISTS public.vehicles (
  id          SERIAL PRIMARY KEY,
  matricula   TEXT NOT NULL UNIQUE,
  marca       TEXT,
  modelo      TEXT,
  ano         INT,
  cor         TEXT,
  estado      TEXT NOT NULL DEFAULT 'ativo',  -- ativo | inativo | vendido | abatido
  -- Ligação polimórfica ao proprietário (person | company | project | ...)
  owner_type  TEXT NOT NULL DEFAULT 'company',
  owner_id    INT,          -- ID interno da entidade proprietária
  entity_id   INT,          -- FK twin_entities (opcional, para empresas/pessoas já no twin)
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN public.vehicles.owner_type IS 'person | company | project';
COMMENT ON COLUMN public.vehicles.owner_id   IS 'ID interno do proprietário (sem FK hard — owner_type define a tabela)';
COMMENT ON COLUMN public.vehicles.entity_id  IS 'Ligação opcional a twin_entities para empresas/pessoas já no twin';

CREATE INDEX IF NOT EXISTS vehicles_owner    ON public.vehicles(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS vehicles_entity   ON public.vehicles(entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS vehicles_estado   ON public.vehicles(estado);
