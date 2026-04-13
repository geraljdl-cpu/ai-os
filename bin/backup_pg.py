#!/usr/bin/env python3
"""
AI-OS Postgres Backup — pg_dump via docker exec + retenção 30 dias.
Ficheiros: ~/ai-os/backups/aios_db_YYYY-MM-DD_HH-MM-SS.sql.gz
Após backup, testa restore num schema temporário para verificar integridade.

CLI:
  python3 backup_pg.py backup           → faz backup agora
  python3 backup_pg.py restore <file>   → restaura para DB aios_db_restore_test
  python3 backup_pg.py cleanup          → apaga backups > RETENTION_DAYS
  python3 backup_pg.py status           → lista backups existentes
"""
import os, sys, json, subprocess, pathlib, datetime, gzip, shutil

AIOS_ROOT      = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
BACKUP_DIR     = AIOS_ROOT / "backups"
RETENTION_DAYS = int(os.environ.get("AIOS_BACKUP_DAYS", "30"))

# Parâmetros Postgres (lidos de ~/.env.db)
_DB_DEFAULTS = {
    "host":     "127.0.0.1",
    "port":     "5432",
    "dbname":   "aios_db",
    "user":     "aios_user",
    "password": "aios2026",
    "container": "postgres",   # nome do container Docker
}

def _load_db_params() -> dict:
    p = _DB_DEFAULTS.copy()
    env_file = pathlib.Path(os.path.expanduser("~/.env.db"))
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "DATABASE_URL=" in line:
                url = line.split("=", 1)[1].strip()
                # postgresql://user:pass@host:port/dbname
                try:
                    rest = url.replace("postgresql+pg8000://", "postgresql://").replace("postgresql://", "")
                    userpass, hostdb = rest.split("@", 1)
                    user, password   = userpass.split(":", 1)
                    hostport, dbname = hostdb.split("/", 1)
                    host, port       = (hostport.split(":") + ["5432"])[:2]
                    p.update({"user": user, "password": password,
                               "host": host, "port": port, "dbname": dbname})
                except Exception:
                    pass
    return p


def _ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")


def _log(msg: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ── Backup ────────────────────────────────────────────────────────────────────

def do_backup() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    p   = _load_db_params()
    out = BACKUP_DIR / f"aios_db_{_ts()}.sql.gz"

    _log(f"A fazer backup → {out.name}")

    # pg_dump via docker exec (evita precisar pg_dump local)
    cmd = [
        "docker", "exec", "-e", f"PGPASSWORD={p['password']}",
        p["container"],
        "pg_dump",
        "-U", p["user"],
        "-h", "localhost",
        "-d", p["dbname"],
        "--no-owner", "--no-acl", "--format=plain",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")
            _log(f"ERRO pg_dump: {err[:300]}")
            return {"ok": False, "error": err[:300]}

        # comprime com gzip
        with gzip.open(str(out), "wb") as f:
            f.write(result.stdout)

        size = out.stat().st_size
        _log(f"Backup OK — {out.name} ({size//1024} KB)")

        # limpeza de backups antigos
        removed = _cleanup()

        return {"ok": True, "file": str(out), "size_bytes": size,
                "removed_old": removed}

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pg_dump timeout (120s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Restore (teste de integridade) ────────────────────────────────────────────

def do_restore(backup_file: str, target_db: str = "aios_db_restore_test") -> dict:
    p    = _load_db_params()
    path = pathlib.Path(backup_file)
    if not path.exists():
        return {"ok": False, "error": f"ficheiro não encontrado: {backup_file}"}

    _log(f"Teste de restore: {path.name} → {target_db}")

    # descomprime para ficheiro temporário
    tmp = BACKUP_DIR / f"_restore_tmp_{_ts()}.sql"
    try:
        with gzip.open(str(path), "rb") as fin, open(str(tmp), "wb") as fout:
            shutil.copyfileobj(fin, fout)
    except Exception as e:
        return {"ok": False, "error": f"decompress error: {e}"}

    def _pgcmd(*args):
        """Executa comando psql sem stdin."""
        full = ["docker", "exec",
                "-e", f"PGPASSWORD={p['password']}",
                p["container"]] + list(args)
        return subprocess.run(full, capture_output=True, timeout=60)

    def _pgpipe(sql_bytes, *args):
        """Pipe de SQL para psql via stdin."""
        full = ["docker", "exec", "-i",
                "-e", f"PGPASSWORD={p['password']}",
                p["container"]] + list(args)
        return subprocess.run(full, input=sql_bytes, capture_output=True, timeout=120)

    try:
        # cria DB de teste (ignora erro se já existe)
        rc = _pgcmd("psql", "-U", p["user"], "-h", "127.0.0.1", "-d", "postgres",
                    "-c", f"CREATE DATABASE {target_db} OWNER {p['user']};")
        if rc.returncode != 0:
            err = rc.stderr.decode(errors="replace")
            # ignora "already exists"
            if "already exists" not in err:
                _log(f"CREATE DATABASE: {err[:200]}")

        # restaura
        sql = tmp.read_bytes()
        r   = _pgpipe(sql, "psql", "-U", p["user"], "-h", "127.0.0.1",
                      "-d", target_db, "--quiet")

        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")
            _log(f"Restore com avisos (normal): {err[:200]}")

        # verifica tabelas
        r2 = _pgcmd("psql", "-U", p["user"], "-h", "127.0.0.1", "-d", target_db,
                    "-c", "SELECT count(*) FROM jobs;")
        ok = r2.returncode == 0
        _log(f"Restore {'OK' if ok else 'FALHOU'}")

        # limpa DB de teste
        _pgcmd("psql", "-U", p["user"], "-h", "127.0.0.1", "-d", "postgres",
               "-c", f"DROP DATABASE IF EXISTS {target_db};")

        return {"ok": ok, "file": str(path), "test_db": target_db,
                "stderr_preview": r.stderr.decode(errors="replace")[:200]}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        tmp.unlink(missing_ok=True)


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _cleanup() -> int:
    cutoff  = datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)
    removed = 0
    for f in BACKUP_DIR.glob("aios_db_*.sql.gz"):
        mtime = datetime.datetime.utcfromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            f.unlink()
            _log(f"Apagado backup antigo: {f.name}")
            removed += 1
    return removed


def do_cleanup() -> dict:
    removed = _cleanup()
    return {"ok": True, "removed": removed}


# ── Status ────────────────────────────────────────────────────────────────────

def do_status() -> dict:
    files = sorted(BACKUP_DIR.glob("aios_db_*.sql.gz"), reverse=True)
    entries = []
    for f in files:
        st = f.stat()
        entries.append({
            "file":       f.name,
            "size_bytes": st.st_size,
            "size_kb":    st.st_size // 1024,
            "mtime":      datetime.datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return {"ok": True, "count": len(entries), "backups": entries,
            "retention_days": RETENTION_DAYS}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "backup":
        result = do_backup()
        if result["ok"]:
            # testa restore
            test = do_restore(result["file"])
            result["restore_test"] = test
        print(json.dumps(result, indent=2))

    elif cmd == "restore" and len(sys.argv) >= 3:
        print(json.dumps(do_restore(sys.argv[2]), indent=2))

    elif cmd == "cleanup":
        print(json.dumps(do_cleanup(), indent=2))

    elif cmd == "status":
        print(json.dumps(do_status(), indent=2))

    else:
        print(json.dumps({"ok": False, "error": "uso: backup|restore <file>|cleanup|status"}))
