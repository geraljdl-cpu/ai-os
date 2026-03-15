-- 350_cluster_node_metrics.sql
-- Cluster node telemetry table — lightweight per-node metrics
-- Collected by bin/cluster_telemetry.py running as systemd timer on each node

CREATE TABLE IF NOT EXISTS public.cluster_node_metrics (
    id            bigserial PRIMARY KEY,
    node          text NOT NULL,
    ip            text,
    cpu_pct       numeric(5,1),
    ram_used_mb   integer,
    ram_total_mb  integer,
    load_1        numeric(6,2),
    disk_used_pct numeric(5,1),
    worker_state  text,             -- idle/working/offline
    current_job_id bigint,
    node_role     text,
    jobs_24h      integer DEFAULT 0,
    failures_24h  integer DEFAULT 0,
    created_at    timestamptz DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS cluster_node_metrics_node_time_idx
    ON public.cluster_node_metrics (node, created_at DESC);

-- Auto-prune rows older than 2 days (keep recent only)
-- Called by cleanup routine or manually
