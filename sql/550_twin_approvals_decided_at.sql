-- Fix: add decided_at column to twin_approvals (was referenced in code but missing from schema)
ALTER TABLE public.twin_approvals ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
