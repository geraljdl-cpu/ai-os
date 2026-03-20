-- Histórico de inbound WhatsApp com alerta para números vigiados
CREATE TABLE IF NOT EXISTS public.whatsapp_inbound_log (
    id           BIGSERIAL PRIMARY KEY,
    from_phone   TEXT        NOT NULL,
    profile_name TEXT,
    body         TEXT,
    client_id    BIGINT,
    watched      BOOLEAN     NOT NULL DEFAULT false,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wail_received ON public.whatsapp_inbound_log(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_wail_phone    ON public.whatsapp_inbound_log(from_phone);
