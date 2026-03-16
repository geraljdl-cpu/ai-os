-- Add next_steps, suggested_tasks and other new columns to existing council_reviews table
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS context TEXT;
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS agents JSONB;
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS synthesis TEXT;
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS next_steps JSONB;
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS suggested_tasks JSONB;
ALTER TABLE public.council_reviews ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'done';

CREATE INDEX IF NOT EXISTS idx_council_reviews_created ON public.council_reviews(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_council_reviews_status  ON public.council_reviews(status);
