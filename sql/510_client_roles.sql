-- Client roles + clients table
-- 2026-03-20

-- Novos roles
INSERT INTO public.roles (name) VALUES ('client_manager')  ON CONFLICT (name) DO NOTHING;
INSERT INTO public.roles (name) VALUES ('client_accounting') ON CONFLICT (name) DO NOTHING;

-- Tabela de clientes
CREATE TABLE IF NOT EXISTS public.clients (
    id            SERIAL PRIMARY KEY,
    name          TEXT    NOT NULL,
    nif           TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ligar utilizador a cliente (NULL = utilizador interno)
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS client_id INTEGER REFERENCES public.clients(id);

CREATE INDEX IF NOT EXISTS idx_users_client_id ON public.users(client_id);
