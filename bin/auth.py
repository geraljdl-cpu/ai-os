#!/usr/bin/env python3
"""
AI-OS JWT Auth — Fase Core
login(username, password) → JWT token
verify_token(token) → user + role
Uso CLI: python3 auth.py login <user> <pass>
         python3 auth.py verify <token>
"""
import os, sys, json, pathlib, importlib.util, datetime

# ── Carrega ~/.env.db ────────────────────────────────────────────────────────
def _load_env():
    p = pathlib.Path(os.path.expanduser("~/.env.db"))
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

JWT_SECRET = os.environ.get("JWT_SECRET", "aios-jwt-secret-2026-change-in-prod")
ALGORITHM  = "HS256"
TOKEN_TTL  = int(os.environ.get("JWT_TTL_HOURS", "24"))

ROLES = ["admin", "supervisor", "operator", "factory", "finance", "viewer", "worker", "show", "cliente",
         "client_manager", "client_accounting"]


# ── Deps ─────────────────────────────────────────────────────────────────────
try:
    from passlib.context import CryptContext
    from jose import JWTError, jwt as _jose_jwt
except ImportError as e:
    sys.exit(f"Dependências em falta: {e}. Corre: pip install passlib[bcrypt] python-jose")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── db loader (lazy) ─────────────────────────────────────────────────────────
_db_mod = None

def _db():
    global _db_mod
    if _db_mod is None:
        _aios = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
        spec  = importlib.util.spec_from_file_location("db", _aios / "bin" / "db.py")
        _db_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_db_mod)
    return _db_mod


# ── Funções públicas ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_token(user_id: int, username: str, role: str, client_id: int = None) -> str:
    payload = {
        "sub":       str(user_id),
        "username":  username,
        "role":      role,
        "exp":       datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_TTL),
    }
    if client_id is not None:
        payload["client_id"] = client_id
    return _jose_jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return _jose_jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])


def login(username: str, password: str) -> dict:
    """Autentica utilizador. Devolve {ok, token, role, username} ou {ok:False, error}."""
    db_mod = _db()
    db = db_mod.SessionLocal()
    try:
        user = (
            db.query(db_mod.User)
            .filter(db_mod.User.username == username, db_mod.User.active == True)
            .first()
        )
        if not user or not verify_password(password, user.hashed_pw):
            return {"ok": False, "error": "credenciais inválidas"}
        role = user.role.name if user.role else "viewer"
        # Get client_id (column added via migration; may not be in ORM model)
        client_id = None
        try:
            from sqlalchemy import text as _text
            row = db.execute(_text("SELECT client_id FROM users WHERE id=:uid"), {"uid": user.id}).fetchone()
            if row and row[0] is not None:
                client_id = int(row[0])
        except Exception:
            pass
        token = create_token(user.id, username, role, client_id=client_id)
        # audit
        try:
            db_mod.audit(db, "login", user_id=user.id, resource=username)
        except Exception:
            pass
        return {"ok": True, "token": token, "role": role, "username": username,
                "client_id": client_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def verify_token(token: str) -> dict:
    """Valida JWT. Devolve {ok, user_id, username, role, client_id?} ou {ok:False, error}."""
    try:
        payload = decode_token(token)
        result = {
            "ok":       True,
            "user_id":  int(payload["sub"]),
            "username": payload["username"],
            "role":     payload["role"],
        }
        if "client_id" in payload and payload["client_id"] is not None:
            result["client_id"] = payload["client_id"]
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_user(username: str, password: str, role: str = "operator",
                client_id: int = None) -> dict:
    """Cria novo utilizador."""
    if role not in ROLES:
        return {"ok": False, "error": f"role inválido. Disponíveis: {ROLES}"}
    db_mod = _db()
    db = db_mod.SessionLocal()
    try:
        existing = db.query(db_mod.User).filter(db_mod.User.username == username).first()
        if existing:
            return {"ok": False, "error": "utilizador já existe"}
        role_obj = db.query(db_mod.Role).filter(db_mod.Role.name == role).first()
        if not role_obj:
            return {"ok": False, "error": f"role '{role}' não encontrado na DB"}
        user = db_mod.User(
            username=username,
            hashed_pw=hash_password(password),
            role_id=role_obj.id,
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        # Set client_id if provided (via raw SQL — column added via migration)
        if client_id is not None:
            from sqlalchemy import text as _text
            db.execute(_text("UPDATE users SET client_id=:cid WHERE id=:uid"),
                       {"cid": client_id, "uid": user.id})
            db.commit()
        try:
            db_mod.audit(db, "create_user", resource=username)
        except Exception:
            pass
        return {"ok": True, "id": user.id, "username": username, "role": role,
                "client_id": client_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def deactivate_user(username: str) -> dict:
    """Desativa utilizador (não apaga)."""
    db_mod = _db()
    db = db_mod.SessionLocal()
    try:
        user = db.query(db_mod.User).filter(db_mod.User.username == username).first()
        if not user:
            return {"ok": False, "error": "utilizador não encontrado"}
        user.active = False
        db.commit()
        return {"ok": True, "username": username, "active": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


def list_users() -> dict:
    """Lista utilizadores (sem hashes)."""
    db_mod = _db()
    db = db_mod.SessionLocal()
    try:
        users = db.query(db_mod.User).all()
        return {
            "ok": True,
            "users": [
                {
                    "id":       u.id,
                    "username": u.username,
                    "role":     u.role.name if u.role else None,
                    "active":   u.active,
                }
                for u in users
            ],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "login" and len(sys.argv) >= 4:
        print(json.dumps(login(sys.argv[2], sys.argv[3])))
    elif cmd == "verify" and len(sys.argv) >= 3:
        print(json.dumps(verify_token(sys.argv[2])))
    elif cmd == "hash" and len(sys.argv) >= 3:
        print(hash_password(sys.argv[2]))
    elif cmd == "users":
        print(json.dumps(list_users()))
    elif cmd == "create" and len(sys.argv) >= 4:
        role      = sys.argv[4] if len(sys.argv) >= 5 else "operator"
        client_id = int(sys.argv[6]) if len(sys.argv) >= 7 and sys.argv[5] == "--client-id" else None
        print(json.dumps(create_user(sys.argv[2], sys.argv[3], role, client_id=client_id)))
    elif cmd == "deactivate" and len(sys.argv) >= 3:
        print(json.dumps(deactivate_user(sys.argv[2])))
    else:
        print("Uso: auth.py login <user> <pass> | verify <token> | hash <pass> | users")
        print("     auth.py create <user> <pass> [role] | deactivate <user>")
