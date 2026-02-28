#!/usr/bin/env python3
"""
AI-OS Secrets Manager — AES-256-GCM local encryption.
Chave mestra em ~/.aios_master_key (600 permissions).
Store em ~/ai-os/runtime/secrets.json (valores cifrados em base64).

API:
  get_secret(name) -> str | None
  set_secret(name, value)
  delete_secret(name)
  list_secrets() -> list[str]

CLI:
  python3 secrets.py get <name>
  python3 secrets.py set <name> <value>
  python3 secrets.py delete <name>
  python3 secrets.py list
  python3 secrets.py import-env <file>   → migra ficheiro .env para secrets
"""
import os, sys, json, base64, pathlib, stat

MASTER_KEY_PATH = pathlib.Path(os.path.expanduser("~/.aios_master_key"))
AIOS_ROOT       = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
STORE_PATH      = AIOS_ROOT / "runtime" / "secrets.json"


# ── Master key ───────────────────────────────────────────────────────────────

def _load_or_create_key() -> bytes:
    if MASTER_KEY_PATH.exists():
        raw = MASTER_KEY_PATH.read_bytes()
        return base64.b64decode(raw.strip())
    # gera nova chave de 32 bytes
    key = os.urandom(32)
    MASTER_KEY_PATH.write_bytes(base64.b64encode(key) + b"\n")
    MASTER_KEY_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    return key


# ── AES-256-GCM ──────────────────────────────────────────────────────────────

def _encrypt(key: bytes, plaintext: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct    = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def _decrypt(key: bytes, ciphertext_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw   = base64.b64decode(ciphertext_b64)
    nonce = raw[:12]
    ct    = raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()


# ── Store ─────────────────────────────────────────────────────────────────────

def _load_store() -> dict:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_store(data: dict):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STORE_PATH) + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, str(STORE_PATH))


# ── Public API ────────────────────────────────────────────────────────────────

def get_secret(name: str) -> str | None:
    store = _load_store()
    enc   = store.get(name)
    if enc is None:
        return None
    key = _load_or_create_key()
    try:
        return _decrypt(key, enc)
    except Exception:
        return None


def set_secret(name: str, value: str):
    key   = _load_or_create_key()
    store = _load_store()
    store[name] = _encrypt(key, value)
    _save_store(store)


def delete_secret(name: str) -> bool:
    store = _load_store()
    if name not in store:
        return False
    del store[name]
    _save_store(store)
    return True


def list_secrets() -> list:
    return list(_load_store().keys())


def import_env_file(filepath: str) -> dict:
    """Migra um ficheiro .env (KEY=VALUE) para o secrets store."""
    p = pathlib.Path(filepath).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"ficheiro não encontrado: {filepath}"}
    imported = []
    skipped  = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            set_secret(k, v)
            imported.append(k)
    return {"ok": True, "imported": imported, "skipped": skipped}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd  = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "get" and len(sys.argv) >= 3:
        v = get_secret(sys.argv[2])
        if v is None:
            print(json.dumps({"ok": False, "error": "not found"}))
        else:
            print(json.dumps({"ok": True, "name": sys.argv[2], "value": v}))

    elif cmd == "set" and len(sys.argv) >= 4:
        set_secret(sys.argv[2], sys.argv[3])
        print(json.dumps({"ok": True, "name": sys.argv[2]}))

    elif cmd == "delete" and len(sys.argv) >= 3:
        ok = delete_secret(sys.argv[2])
        print(json.dumps({"ok": ok}))

    elif cmd == "list":
        print(json.dumps({"ok": True, "secrets": list_secrets()}))

    elif cmd == "import-env" and len(sys.argv) >= 3:
        print(json.dumps(import_env_file(sys.argv[2])))

    else:
        print(json.dumps({"ok": False, "error": "uso: get|set|delete|list|import-env"}))
