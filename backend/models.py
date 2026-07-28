"""
Full schema, running on real PostgreSQL (tested against a local
instance in dev -- see README for connection setup).
"""

import enum
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey,
    Enum as SAEnum, Text, JSON,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from config import settings

db_url = settings.database_url
if "postgresql" in db_url:
    try:
        import socket
        s = socket.socket()
        s.settimeout(1.0)
        s.connect(("127.0.0.1", 5432))
        s.close()
    except Exception:
        print("[DB] PostgreSQL not running on 127.0.0.1:5432. Falling back to SQLite: sqlite:///./monitoring.db")
        db_url = "sqlite:///./monitoring.db"

engine_args = {}
if db_url.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}
else:
    engine_args["pool_pre_ping"] = True

engine = create_engine(db_url, **engine_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Role(str, enum.Enum):
    SUPERADMIN = "SuperAdmin"
    ITSTAFF = "ITStaff"
    MANAGER = "Manager"
    VIEWER = "Viewer"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(SAEnum(Role, native_enum=False, length=20), nullable=False)
    display_name = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # For Manager/Viewer roles: which employees they're allowed to see.
    # Empty list for IT/SuperAdmin means "all" (enforced in RBAC layer).
    managed_employee_ids = Column(JSON, default=list)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    employee_id = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    hostname = Column(String)
    ip_address = Column(String)  # captured from the actual connection, not self-reported
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=True)


class ConsentLog(Base):
    __tablename__ = "consent_log"
    id = Column(Integer, primary_key=True)
    employee_id = Column(String, nullable=False, index=True)
    hostname = Column(String, nullable=False)
    policy_version = Column(String, nullable=False)
    accepted_at = Column(DateTime, nullable=False)


class Screenshot(Base):
    __tablename__ = "screenshots"
    id = Column(Integer, primary_key=True)
    employee_id = Column(String, nullable=False, index=True)
    captured_at = Column(DateTime, nullable=False)
    stored_path = Column(String, nullable=False)
    uploaded_at = Column(DateTime, nullable=False)


class ActivityEvent(Base):
    """
    Feeds the Activity Monitor: browser history, USB connect/disconnect,
    file operations, flagged app launches. Deliberately separate from
    Screenshot -- this is structured, searchable data (a URL, a file
    path, a device name), which is a different sensitivity tier than a
    screenshot someone has to look at to read -- see require_it gating
    on the /activity read endpoint in main.py.
    """
    __tablename__ = "activity_events"
    id = Column(Integer, primary_key=True)
    employee_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)  # BROWSER | USB | FILE | APP | TELEMETRY
    severity = Column(String, nullable=False, default="info")  # info | warn | critical
    summary = Column(String, nullable=False)  # short human-readable line for the Details column
    detail = Column(JSON, default=dict)  # structured extra fields (url, device, path, etc.)
    occurred_at = Column(DateTime, nullable=False)  # when the agent observed it
    logged_at = Column(DateTime, default=datetime.utcnow)  # when the backend received it


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    rule_type = Column(String, nullable=False)  # idle_minutes | after_hours | blacklisted_app
    # e.g. {"minutes": 30} / {"start": "18:00", "end": "08:00"} / {"app_name": "steam.exe"}
    params = Column(JSON, nullable=False)
    notify_via = Column(String, nullable=False, default="webhook")  # webhook | smtp
    notify_target = Column(String, nullable=False)  # webhook URL or email address
    enabled = Column(Boolean, default=True)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class AlertEvent(Base):
    """A fired alert -- what the Manager UI reads to show plain-language cards."""
    __tablename__ = "alert_events"
    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id"))
    employee_id = Column(String, nullable=False, index=True)
    message = Column(String, nullable=False)  # plain-language, e.g. "Ahmed idle 30 min"
    fired_at = Column(DateTime, default=datetime.utcnow)
    acknowledged = Column(Boolean, default=False)


class AuditLog(Base):
    """
    Every view of employee data, and every remote session action, lands
    here automatically. This is a feature (accountability trail), not
    just a compliance checkbox -- see log_audit() in audit.py.
    """
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    actor_username = Column(String, nullable=False, index=True)
    actor_role = Column(String, nullable=False)
    action = Column(String, nullable=False)  # e.g. "view_screenshots", "remote_session_start"
    target_employee_id = Column(String, index=True)
    detail = Column(JSON, default=dict)
    at = Column(DateTime, default=datetime.utcnow)


class RemoteSession(Base):
    __tablename__ = "remote_sessions"
    id = Column(Integer, primary_key=True)
    employee_id = Column(String, nullable=False, index=True)
    started_by = Column(String, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    blank_screen_seconds = Column(Integer, default=0)  # cumulative time blanked


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
