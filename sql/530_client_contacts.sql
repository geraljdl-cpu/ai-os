-- Contactos do cliente (separados de workers)
CREATE TABLE IF NOT EXISTS public.client_contacts (
    id                      BIGSERIAL    PRIMARY KEY,
    client_id               BIGINT       NOT NULL REFERENCES public.clients(id) ON DELETE CASCADE,
    name                    TEXT         NOT NULL,
    phone                   TEXT         UNIQUE,
    email                   TEXT,
    role                    TEXT         NOT NULL DEFAULT 'manager',
    is_primary              BOOLEAN      NOT NULL DEFAULT false,
    can_approve_timesheets  BOOLEAN      NOT NULL DEFAULT true,
    can_approve_expenses    BOOLEAN      NOT NULL DEFAULT true,
    can_view_financials     BOOLEAN      NOT NULL DEFAULT false,
    can_receive_whatsapp    BOOLEAN      NOT NULL DEFAULT true,
    can_receive_email       BOOLEAN      NOT NULL DEFAULT true,
    notes                   TEXT,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cc_client_id ON public.client_contacts(client_id);
CREATE INDEX IF NOT EXISTS idx_cc_phone     ON public.client_contacts(phone);

-- Seed: Frederico (aprovador primário, Maccan)
INSERT INTO public.client_contacts
    (client_id, name, phone, role, is_primary,
     can_approve_timesheets, can_approve_expenses, can_receive_whatsapp)
VALUES
    (1, 'Frederico', '+351913002441', 'manager', true,
     true, true, true)
ON CONFLICT (phone) DO NOTHING;

-- Seed: Ana Pereira (contabilidade, Maccan)
INSERT INTO public.client_contacts
    (client_id, name, email, role, is_primary,
     can_approve_timesheets, can_view_financials, can_receive_email, can_receive_whatsapp)
VALUES
    (1, 'Ana Pereira', 'ana.pereira@maccan.pt', 'accounting', false,
     false, true, true, false)
ON CONFLICT DO NOTHING;

-- Acrescentar contact_type e contact_id ao log de inbound (se não existirem)
ALTER TABLE public.whatsapp_inbound_log
    ADD COLUMN IF NOT EXISTS contact_type TEXT,
    ADD COLUMN IF NOT EXISTS contact_id   BIGINT;
