-- 001_schema_patches.sql
-- Patches aplicados na sessão 2026-03-15 para alinhar schema com noc_query.py
-- Seguro correr múltiplas vezes (IF NOT EXISTS / SET DEFAULT idempotente)

-- 1. public.events — colunas adicionais usadas por twin_core e noc_query
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS level      TEXT NOT NULL DEFAULT 'info';
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS kind       TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS entity_id  BIGINT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS message    TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS data       JSONB NOT NULL DEFAULT '{}';
-- type é NOT NULL mas sem default no schema base — noc_query não fornece valor
ALTER TABLE public.events ALTER COLUMN type SET DEFAULT 'event';

-- 2. public.twin_approvals — coluna summary usada por twin_batch_faturar
ALTER TABLE public.twin_approvals ADD COLUMN IF NOT EXISTS summary TEXT;
