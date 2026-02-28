#!/usr/bin/env python3
"""
AI-OS Database — SQLAlchemy 2.x + Postgres
Tabelas: users, roles, jobs, steps, approvals, audit_log
audit_log é imutável (apenas INSERT — UPDATE/DELETE bloqueados via evento ORM).
"""
import os, pathlib, datetime
from sqlalchemy import (
    create_engine, event, Column, Integer, String, Text,
    DateTime, Boolean, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship


def _load_env_db():
    p = pathlib.Path(os.path.expanduser("~/.env.db"))
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env_db()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:aios2026@127.0.0.1:5432/aios",
)

engine       = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base         = declarative_base()


# ── Models ──────────────────────────────────────────────────────────────────

class Role(Base):
    __tablename__ = "roles"
    id   = Column(Integer, primary_key=True)
    name = Column(String(32), unique=True, nullable=False)


class User(Base):
    __tablename__ = "users"
    id         = Column(Integer, primary_key=True)
    username   = Column(String(64), unique=True, nullable=False)
    hashed_pw  = Column(String(256), nullable=False)
    active     = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    role_id    = Column(Integer, ForeignKey("roles.id"), nullable=True)
    role       = relationship("Role")


class Job(Base):
    __tablename__ = "jobs"
    id         = Column(String(64), primary_key=True)
    goal       = Column(Text, nullable=True)
    status     = Column(String(32), default="pending")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class Step(Base):
    __tablename__ = "steps"
    id     = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), ForeignKey("jobs.id"), nullable=False)
    tool   = Column(String(64), nullable=False)
    input  = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    ts     = Column(DateTime, default=datetime.datetime.utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id           = Column(String(64), primary_key=True)
    tool         = Column(String(64), nullable=False)
    input        = Column(Text, nullable=True)
    status       = Column(String(32), default="pending")
    job_id       = Column(String(64), nullable=True)
    requested_at = Column(DateTime, default=datetime.datetime.utcnow)
    resolved_at  = Column(DateTime, nullable=True)
    resolved_by  = Column(Integer, ForeignKey("users.id"), nullable=True)


class AuditLog(Base):
    """Tabela imutável — apenas INSERT é permitido."""
    __tablename__ = "audit_log"
    id       = Column(Integer, primary_key=True)
    ts       = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    user_id  = Column(Integer, nullable=True)
    action   = Column(String(64), nullable=False)
    resource = Column(String(128), nullable=True)
    detail   = Column(Text, nullable=True)
    ip       = Column(String(64), nullable=True)


# ── Guardiões de imutabilidade do audit_log ─────────────────────────────────

@event.listens_for(AuditLog, "before_update")
def _no_audit_update(mapper, connection, target):
    raise RuntimeError("audit_log é imutável — UPDATE não permitido")


@event.listens_for(AuditLog, "before_delete")
def _no_audit_delete(mapper, connection, target):
    raise RuntimeError("audit_log é imutável — DELETE não permitido")


# ── Helper ───────────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def audit(db, action: str, user_id=None, resource=None, detail=None, ip=None):
    entry = AuditLog(
        action=action, user_id=user_id,
        resource=resource, detail=detail, ip=ip,
    )
    db.add(entry)
    db.commit()
