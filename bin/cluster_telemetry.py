#!/usr/bin/env python3
"""
cluster_telemetry.py — Cluster Node Metrics Collector
Runs every 30s via systemd timer on each cluster node.
Inserts one row into public.cluster_node_metrics.

Env vars:
  AIOS_ROOT       — /cluster/d1/ai-os (default)
  DATABASE_URL    — override DB URL
"""
import sys, os

_ROOT  = os.environ.get("AIOS_ROOT", "/cluster/d1/ai-os")
_PYLIB = os.path.join(_ROOT, "pylib")
_BINDIR = os.path.join(_ROOT, "bin")
if os.path.isdir(_PYLIB) and _PYLIB not in sys.path:
    sys.path.insert(0, _PYLIB)
if _BINDIR in sys.path:
    sys.path.remove(_BINDIR)

import json, socket, time, pathlib

NODE = os.environ.get("AIOS_NODE_NAME", socket.gethostname())
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+pg8000://aios_user:jdl@192.168.1.201:5432/aios"
)

# ── Load config ──────────────────────────────────────────────────────────────
_cfg_path = pathlib.Path(_ROOT) / "config" / "cluster_workers.json"
try:
    _cfg  = json.loads(_cfg_path.read_text())
    _node_cfg = _cfg.get("nodes", {}).get(NODE, {})
    _IP   = _node_cfg.get("ip", "")
    _ROLES = ",".join(_node_cfg.get("roles", []))
except Exception:
    _IP = ""
    _ROLES = ""


def _cpu_pct():
    """Read /proc/stat for CPU usage (1-sample approx via idle%)."""
    try:
        lines = open("/proc/stat").readlines()
        for line in lines:
            if line.startswith("cpu "):
                parts = list(map(int, line.split()[1:]))
                idle = parts[3]
                total = sum(parts)
                # Second sample after 200ms for accurate reading
                time.sleep(0.2)
                lines2 = open("/proc/stat").readlines()
                for l2 in lines2:
                    if l2.startswith("cpu "):
                        p2 = list(map(int, l2.split()[1:]))
                        d_idle = p2[3] - idle
                        d_total = sum(p2) - total
                        if d_total == 0:
                            return 0.0
                        return round(100.0 * (1 - d_idle / d_total), 1)
    except Exception:
        pass
    return None


def _mem():
    """Returns (used_mb, total_mb) from /proc/meminfo."""
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", 0) // 1024
        used  = total - avail
        return used, total
    except Exception:
        return None, None


def _load():
    """Returns load average (1min) from /proc/loadavg."""
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except Exception:
        return None


def _disk_pct():
    """Disk usage % for /cluster mount."""
    try:
        import shutil
        total, used, free = shutil.disk_usage("/cluster")
        return round(100.0 * used / total, 1)
    except Exception:
        return None


def _worker_state():
    """Check current worker state and job from cluster_worker logs."""
    try:
        log_file = pathlib.Path(_ROOT) / "runtime" / "workers" / NODE / "worker.log"
        if not log_file.exists():
            return "offline", None, None
        # Read last 10 lines
        with open(log_file) as f:
            lines = f.readlines()[-10:]
        for line in reversed(lines):
            if ">>> Job" in line:
                # Found a running or recent job
                parts = line.strip().split()
                try:
                    jid = int(parts[parts.index("Job") + 1])
                    return "working", jid, None
                except Exception:
                    return "working", None, None
            if "<<< Job" in line and "done" in line:
                return "idle", None, None
            if "Worker pronto" in line or "idle" in line.lower():
                return "idle", None, None
        return "idle", None, None
    except Exception:
        return "unknown", None, None


def _job_stats():
    """Count jobs_24h and failures_24h from DB."""
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(DATABASE_URL, pool_pre_ping=False, pool_size=1)
        with eng.connect() as c:
            row = c.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('done','failed') AND ts_done > NOW() - INTERVAL '24 hours') AS total,
                    COUNT(*) FILTER (WHERE status = 'failed' AND ts_done > NOW() - INTERVAL '24 hours') AS fails
                FROM public.worker_jobs
                WHERE assigned_worker_id = :node
            """), {"node": NODE}).mappings().first()
        return int(row["total"] or 0), int(row["fails"] or 0)
    except Exception:
        return 0, 0


def collect():
    cpu   = _cpu_pct()
    ram_u, ram_t = _mem()
    load  = _load()
    disk  = _disk_pct()
    state, job_id, role = _worker_state()
    jobs_24h, fails_24h = _job_stats()

    row = {
        "node":         NODE,
        "ip":           _IP,
        "cpu_pct":      cpu,
        "ram_used_mb":  ram_u,
        "ram_total_mb": ram_t,
        "load_1":       load,
        "disk_used_pct": disk,
        "worker_state": state,
        "current_job_id": job_id,
        "node_role": _ROLES,
        "jobs_24h":     jobs_24h,
        "failures_24h": fails_24h,
    }
    return row


def insert(row):
    from sqlalchemy import create_engine, text
    eng = create_engine(DATABASE_URL, pool_pre_ping=False, pool_size=1)
    with eng.begin() as c:
        c.execute(text("""
            INSERT INTO public.cluster_node_metrics
                (node, ip, cpu_pct, ram_used_mb, ram_total_mb, load_1,
                 disk_used_pct, worker_state, current_job_id, node_role,
                 jobs_24h, failures_24h)
            VALUES
                (:node, :ip, :cpu_pct, :ram_used_mb, :ram_total_mb, :load_1,
                 :disk_used_pct, :worker_state, :current_job_id, :node_role,
                 :jobs_24h, :failures_24h)
        """), row)


if __name__ == "__main__":
    row = collect()
    insert(row)
    print(json.dumps({k: v for k, v in row.items() if v is not None}))
