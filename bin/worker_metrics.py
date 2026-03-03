#!/usr/bin/env python3
"""
AI-OS Worker Metrics
Lê runtime/worker.last_seen, calcula tempo desde último ciclo,
escreve runtime/worker_metrics.json.
Usa apenas python3 stdlib.
"""
import os, json, datetime, pathlib

AIOS_ROOT   = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
LAST_SEEN   = AIOS_ROOT / "runtime" / "worker.last_seen"
OUT_FILE    = AIOS_ROOT / "runtime" / "worker_metrics.json"

STATUS_OK      = "ok"
STATUS_STALE   = "stale"    # > 60s sem ciclo
STATUS_DEAD    = "dead"     # > 300s sem ciclo
STATUS_UNKNOWN = "unknown"  # ficheiro não existe


def generate() -> dict:
    now = datetime.datetime.utcnow()
    now_ts = now.timestamp()

    last_seen_str = None
    last_seen_ts  = None
    age_secs      = None
    status        = STATUS_UNKNOWN

    if LAST_SEEN.exists():
        try:
            # toma a última linha não vazia (pode haver duplicados de ciclos anteriores)
            lines = [l.strip() for l in LAST_SEEN.read_text(encoding="utf-8").splitlines() if l.strip()]
            raw = lines[-1] if lines else ""
            # suporta ISO format (2026-03-03T04:00:00) e unix timestamp
            try:
                # fromisoformat não aceita 'Z' em Python < 3.11; normaliza
                iso = raw.replace("Z", "+00:00")
                dt  = datetime.datetime.fromisoformat(iso)
                # converte para UTC naive para cálculo consistente
                if dt.tzinfo is not None:
                    dt = dt.utctimetuple()
                    dt = datetime.datetime(*dt[:6])
                last_seen_ts = dt.timestamp()
            except ValueError:
                last_seen_ts = float(raw)
                dt = datetime.datetime.utcfromtimestamp(last_seen_ts)
            last_seen_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            age_secs = int(now_ts - last_seen_ts)

            if age_secs <= 60:
                status = STATUS_OK
            elif age_secs <= 300:
                status = STATUS_STALE
            else:
                status = STATUS_DEAD
        except Exception as e:
            status = STATUS_UNKNOWN
            last_seen_str = f"parse error: {e}"

    metrics = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":       status,
        "last_seen":    last_seen_str,
        "age_secs":     age_secs,
        "thresholds":   {"ok": 60, "stale": 300},
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    return metrics


if __name__ == "__main__":
    m = generate()
    status = m["status"].upper()
    age    = m["age_secs"]
    last   = m["last_seen"] or "—"
    icon   = {"OK": "✓", "STALE": "⚠", "DEAD": "✗", "UNKNOWN": "?"}.get(status, "?")
    print(f"{icon} worker status={status}  last_seen={last}  age={age}s")
    print(json.dumps(m, indent=2, ensure_ascii=False))
