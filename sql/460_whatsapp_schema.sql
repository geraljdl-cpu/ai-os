-- 460_whatsapp_schema.sql — Ponto WhatsApp: extensões DB

-- Estender event_timesheets para suportar punch-in/out via WhatsApp
ALTER TABLE public.event_timesheets
  ADD COLUMN IF NOT EXISTS worker_phone TEXT,
  ADD COLUMN IF NOT EXISTS client_phone TEXT,
  ADD COLUMN IF NOT EXISTS check_out_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS gps_lat      NUMERIC(10,7),
  ADD COLUMN IF NOT EXISTS gps_lon      NUMERIC(10,7),
  ADD COLUMN IF NOT EXISTS gps_source   TEXT;

-- Comentários dos novos campos
COMMENT ON COLUMN public.event_timesheets.worker_phone  IS 'Número WhatsApp do colaborador';
COMMENT ON COLUMN public.event_timesheets.client_phone  IS 'Número WhatsApp do cliente para notificação';
COMMENT ON COLUMN public.event_timesheets.check_out_at  IS 'Timestamp saída (fim do turno)';
COMMENT ON COLUMN public.event_timesheets.gps_lat       IS 'Latitude GPS da localização (WhatsApp location)';
COMMENT ON COLUMN public.event_timesheets.gps_lon       IS 'Longitude GPS da localização (WhatsApp location)';
COMMENT ON COLUMN public.event_timesheets.gps_source    IS 'Fonte do GPS: whatsapp_gps | manual';

-- status 'active' = turno aberto (punch-in sem punch-out)
-- Não é necessário alterar constraints: status é TEXT sem enum

-- Tabela de contactos WhatsApp dos colaboradores
CREATE TABLE IF NOT EXISTS public.worker_contacts (
  id                   SERIAL PRIMARY KEY,
  worker_name          TEXT NOT NULL,
  whatsapp_phone       TEXT NOT NULL UNIQUE,   -- formato +351XXXXXXXXX
  active               BOOLEAN NOT NULL DEFAULT TRUE,
  people_id            INT REFERENCES public.persons(id),
  default_client_phone TEXT,                   -- WhatsApp cliente (link validação)
  default_client_email TEXT,                   -- Email contabilidade cliente (pós-aprovação)
  notes                TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.worker_contacts ADD COLUMN IF NOT EXISTS default_client_email TEXT;

COMMENT ON TABLE public.worker_contacts IS 'Mapeamento número WhatsApp → colaborador (persons)';
