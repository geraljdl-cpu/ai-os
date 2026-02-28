#!/usr/bin/env python3
"""
AI-OS DB Migration — cria tabelas e utilizador admin inicial.
Uso: python3 bin/migrate.py
"""
import os, sys, pathlib, importlib.util

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    print("=== AI-OS DB Migration ===")
    db_mod   = _load("db",   AIOS_ROOT / "bin" / "db.py")
    auth_mod = _load("auth", AIOS_ROOT / "bin" / "auth.py")

    print(f"DB: {db_mod.DATABASE_URL.split('@')[1] if '@' in db_mod.DATABASE_URL else db_mod.DATABASE_URL}")

    print("Criando tabelas...")
    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    print("Tabelas criadas: roles, users, jobs, steps, approvals, audit_log")

    db = db_mod.SessionLocal()
    try:
        # Roles
        roles_needed = ["admin", "operator", "viewer", "finance", "factory", "show"]
        for r_name in roles_needed:
            if not db.query(db_mod.Role).filter(db_mod.Role.name == r_name).first():
                db.add(db_mod.Role(name=r_name))
        db.commit()
        print(f"Roles: {roles_needed}")

        # Admin
        if not db.query(db_mod.User).filter(db_mod.User.username == "admin").first():
            admin_role = db.query(db_mod.Role).filter(db_mod.Role.name == "admin").first()
            db.add(db_mod.User(
                username="admin",
                hashed_pw=auth_mod.hash_password("aios2026"),
                active=True,
                role=admin_role,
            ))
            db.commit()
            print("Utilizador criado: admin / aios2026")
        else:
            print("Utilizador admin já existe.")

        # Audit log de arranque
        db_mod.audit(db, "migrate", resource="system", detail="migration run")
        print("audit_log: entrada de arranque registada.")

    finally:
        db.close()

    print("=== Migration concluída ===")


if __name__ == "__main__":
    run()
