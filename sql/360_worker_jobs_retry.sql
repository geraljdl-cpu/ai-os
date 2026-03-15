-- 360_worker_jobs_retry.sql
-- Add retry support to worker_jobs

ALTER TABLE public.worker_jobs
  ADD COLUMN IF NOT EXISTS retry_count  INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_retries  INT NOT NULL DEFAULT 2;

CREATE INDEX IF NOT EXISTS worker_jobs_retry
  ON public.worker_jobs(status, retry_count, ts_assigned)
  WHERE status IN ('running','failed');

COMMENT ON COLUMN public.worker_jobs.retry_count IS 'Number of times this job has been retried';
COMMENT ON COLUMN public.worker_jobs.max_retries IS 'Max retries before permanent failure (default 2)';
