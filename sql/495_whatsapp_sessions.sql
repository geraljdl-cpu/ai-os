-- Tracking janela de 24h por número (WhatsApp inbound sessions)
-- 2026-03-20

CREATE TABLE IF NOT EXISTS public.whatsapp_sessions (
    phone           TEXT PRIMARY KEY,
    last_inbound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Flag de canal de entrega em event_timesheets
-- null = não enviado | whatsapp_session | whatsapp_template | whatsapp_63016 | email_fallback
ALTER TABLE public.event_timesheets
    ADD COLUMN IF NOT EXISTS delivery_status TEXT;
