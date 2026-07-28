import asyncio
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from config import settings
from models import (
    init_db, get_db, User, Role, Employee, ConsentLog, Screenshot, ActivityEvent,
    AlertRule, AlertEvent, AuditLog, RemoteSession,
)
from auth import (
    hash_password, verify_password, create_access_token, get_current_user, decode_token,
    require_admin, require_it, require_read, require_gallery, visible_employee_ids,
)
from audit import log_audit
from presence import update_presence, get_presence, get_all_presence
from ws import console_manager, remote_manager
from alerts import alert_engine_loop

STORAGE_ROOT = Path(settings.storage_root)

app = FastAPI(title="Monitoring Backend - Full")


@app.on_event("startup")
async def startup():
    STORAGE_ROOT.mkdir(exist_ok=True, parents=True)
    init_db()
    await console_manager.start_redis_listener()
    asyncio.create_task(alert_engine_loop())

    # Bootstrap a SuperAdmin on first run if none exists, so there's always
    # a way in. Change this password immediately in a real deployment.
    db = next(get_db())
    if not db.query(User).filter(User.role == Role.SUPERADMIN).first():
        db.add(User(
            username="admin",
            password_hash=hash_password("changeme123"),
            role=Role.SUPERADMIN,
            display_name="Default Admin",
        ))
        db.commit()
    db.close()


# ---------- Auth ----------

@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form.username, User.active == True).first()  # noqa: E712
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "role": user.role.value, "display_name": user.display_name}


# ---------- Consent (called by installer/agent, no auth -- pre-login by design) ----------

@app.post("/consent")
def log_consent(request: Request, employee_id: str = Form(...), hostname: str = Form(...),
                 policy_version: str = Form(...), db: Session = Depends(get_db)):
    accepted_at = datetime.utcnow()
    # Real network-level source IP -- this is what actually connected,
    # not a value the agent self-reports and could get wrong or fake.
    source_ip = request.client.host if request.client else None

    db.add(ConsentLog(employee_id=employee_id, hostname=hostname,
                       policy_version=policy_version, accepted_at=accepted_at))

    existing = db.query(Employee).filter(Employee.employee_id == employee_id).first()
    if not existing:
        db.add(Employee(employee_id=employee_id, display_name=employee_id,
                         hostname=hostname, ip_address=source_ip, last_seen_at=accepted_at))
    else:
        # Machine reconnecting / IP changed (e.g. new Tailscale lease) --
        # keep this fresh automatically rather than requiring a manual edit.
        existing.hostname = hostname
        existing.ip_address = source_ip
        existing.last_seen_at = accepted_at
    db.commit()
    return {"status": "recorded", "accepted_at": accepted_at.isoformat()}


@app.get("/consent/{employee_id}")
def has_consented(employee_id: str, db: Session = Depends(get_db)):
    row = (
        db.query(ConsentLog)
        .filter(ConsentLog.employee_id == employee_id)
        .order_by(ConsentLog.id.desc())
        .first()
    )
    return {"consented": bool(row), "accepted_at": row.accepted_at.isoformat() if row else None}


# ---------- Screenshots ----------

@app.post("/upload_screenshot")
async def upload_screenshot(employee_id: str = Form(...), captured_at: str = Form(...),
                             file: UploadFile = File(...), db: Session = Depends(get_db)):
    consented = db.query(ConsentLog).filter(ConsentLog.employee_id == employee_id).first()
    if not consented:
        raise HTTPException(status_code=403, detail="No consent on file for this employee_id")

    day_folder = STORAGE_ROOT / employee_id / date.today().isoformat()
    day_folder.mkdir(parents=True, exist_ok=True)
    safe_ts = captured_at.replace(":", "-")
    dest_path = day_folder / f"{safe_ts}.png"
    with dest_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    db.add(Screenshot(employee_id=employee_id, captured_at=datetime.fromisoformat(captured_at),
                       stored_path=str(dest_path), uploaded_at=datetime.utcnow()))
    db.commit()
    return {"status": "stored", "path": str(dest_path)}


@app.get("/screenshots/{employee_id}")
def list_screenshots(employee_id: str, user: User = Depends(require_gallery), db: Session = Depends(get_db)):
    allowed = visible_employee_ids(user, db)
    if allowed is not None and employee_id not in allowed:
        raise HTTPException(status_code=403, detail="Not permitted to view this employee")

    rows = (
        db.query(Screenshot)
        .filter(Screenshot.employee_id == employee_id)
        .order_by(Screenshot.id.desc())
        .all()
    )
    log_audit(db, user, "view_screenshots", target_employee_id=employee_id, detail={"count": len(rows)})
    return [{"id": r.id, "captured_at": r.captured_at.isoformat(), "stored_path": r.stored_path,
              "uploaded_at": r.uploaded_at.isoformat()} for r in rows]


@app.get("/screenshots/{employee_id}/{screenshot_id}/image")
def get_screenshot_image(employee_id: str, screenshot_id: int,
                          user: User = Depends(require_gallery), db: Session = Depends(get_db)):
    """
    Serves the actual image bytes for one screenshot. The console never
    reads stored_path off the filesystem directly -- consoles run on a
    different machine than the backend in production, so the only way
    they can see a picture is through this endpoint.
    """
    allowed = visible_employee_ids(user, db)
    if allowed is not None and employee_id not in allowed:
        raise HTTPException(status_code=403, detail="Not permitted to view this employee")
    row = (
        db.query(Screenshot)
        .filter(Screenshot.id == screenshot_id, Screenshot.employee_id == employee_id)
        .first()
    )
    if not row or not Path(row.stored_path).exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(row.stored_path, media_type="image/png")


@app.get("/screenshots/{employee_id}/latest")
def get_latest_screenshot_image(employee_id: str,
                                 user: User = Depends(require_gallery), db: Session = Depends(get_db)):
    """
    Serves the most recent screenshot for one employee -- this is what
    powers the live thumbnail wall. Deliberately NOT written to the
    audit log: the wall polls every few seconds per employee, and
    logging every poll would flood the audit trail with noise that
    has no accountability value. Opening the actual screenshot gallery
    or a remote session still logs, per the audit requirement.
    """
    allowed = visible_employee_ids(user, db)
    if allowed is not None and employee_id not in allowed:
        raise HTTPException(status_code=403, detail="Not permitted to view this employee")
    row = (
        db.query(Screenshot)
        .filter(Screenshot.employee_id == employee_id)
        .order_by(Screenshot.id.desc())
        .first()
    )
    if not row or not Path(row.stored_path).exists():
        raise HTTPException(status_code=404, detail="No screenshot yet")
    return FileResponse(row.stored_path, media_type="image/png")


# ---------- Live status ----------

# ---------- Activity Monitor (browser history, USB, file ops, flagged apps) ----------

@app.post("/activity")
def log_activity(employee_id: str = Form(...), event_type: str = Form(...),
                  severity: str = Form("info"), summary: str = Form(...),
                  occurred_at: str = Form(...), detail: str = Form("{}"),
                  db: Session = Depends(get_db)):
    """Agent -> backend, consent-gated like /upload_screenshot -- no user
    JWT here, this is the agent itself reporting, not a console read."""
    consented = db.query(ConsentLog).filter(ConsentLog.employee_id == employee_id).first()
    if not consented:
        raise HTTPException(status_code=403, detail="No consent on file for this employee_id")
    if event_type not in ("BROWSER", "USB", "FILE", "APP", "TELEMETRY"):
        raise HTTPException(status_code=400, detail="Unknown event_type")
    try:
        detail_obj = json.loads(detail)
    except json.JSONDecodeError:
        detail_obj = {}
    dt = datetime.fromisoformat(occurred_at) if occurred_at else datetime.utcnow()
    db.add(ActivityEvent(
        employee_id=employee_id, event_type=event_type, severity=severity,
        summary=summary, detail=detail_obj, occurred_at=dt,
    ))
    if severity in ("warn", "critical"):
        rule = db.query(AlertRule).first()
        rule_id = rule.id if rule else 1
        db.add(AlertEvent(
            rule_id=rule_id,
            employee_id=employee_id,
            message=f"[{severity.upper()}] {summary}",
            fired_at=dt,
        ))
    db.commit()
    return {"status": "recorded"}


@app.get("/activity/{employee_id}")
def list_activity(employee_id: str, event_type: Optional[str] = None, severity: Optional[str] = None,
                   limit: int = 200, user: User = Depends(require_it), db: Session = Depends(get_db)):
    """
    IT/SuperAdmin only -- see the class docstring on ActivityEvent for
    why this is gated tighter than the screenshot gallery. employee_id
    of 'all' returns every employee's events (for the unfiltered wall
    view); a specific id filters to just them.
    """
    query = db.query(ActivityEvent)
    if employee_id != "all":
        query = query.filter(ActivityEvent.employee_id == employee_id)
    if event_type and event_type != "All Types":
        query = query.filter(ActivityEvent.event_type == event_type)
    if severity and severity != "All Severities":
        query = query.filter(ActivityEvent.severity == severity)
    rows = query.order_by(ActivityEvent.id.desc()).limit(min(limit, 1000)).all()
    log_audit(db, user, "view_activity", target_employee_id=employee_id,
              detail={"event_type": event_type, "severity": severity, "count": len(rows)})
    return [{"id": r.id, "employee_id": r.employee_id, "event_type": r.event_type,
              "severity": r.severity, "summary": r.summary, "detail": r.detail,
              "occurred_at": r.occurred_at.isoformat()} for r in rows]


# ---------- Overview dashboard ----------

def _ip_location_status(ip: Optional[str]) -> str:
    """Rough heuristic from the address shape alone -- Tailscale's CGNAT
    range (100.64.0.0/10) reads as VPN, RFC1918 private ranges read as
    OFFICE (on the local LAN), anything else as WAN. This is a label for
    a human glancing at a table, not a security boundary -- don't wire
    anything access-control-relevant to it."""
    if not ip:
        return "UNKNOWN"
    if ip.startswith("100.") or ip.startswith("127."):
        return "VPN"
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
        return "OFFICE"
    return "WAN"


@app.get("/overview/summary")
def overview_summary(user: User = Depends(require_read), db: Session = Depends(get_db)):
    allowed = visible_employee_ids(user, db)
    q = db.query(Employee)
    if allowed is not None:
        q = q.filter(Employee.employee_id.in_(allowed))
    employees = q.order_by(Employee.last_seen_at.desc().nullslast()).all()

    presences = get_all_presence([e.employee_id for e in employees])
    online_count = sum(1 for p in presences if p.get("status") in ("active", "idle"))

    active_sessions = db.query(RemoteSession).filter(RemoteSession.ended_at.is_(None))
    if allowed is not None:
        active_sessions = active_sessions.filter(RemoteSession.employee_id.in_(allowed))
    active_sessions_count = active_sessions.count()

    since = datetime.utcnow() - timedelta(hours=24)
    critical_q = db.query(ActivityEvent).filter(
        ActivityEvent.severity == "critical", ActivityEvent.occurred_at >= since)
    if allowed is not None:
        critical_q = critical_q.filter(ActivityEvent.employee_id.in_(allowed))
    critical_alerts_24h = critical_q.count()

    audits = []
    for e in employees[:10]:
        p = get_presence(e.employee_id)
        audits.append({
            "employee_id": e.employee_id, "display_name": e.display_name,
            "ip_address": e.ip_address, "ip_type": "WAN" if _ip_location_status(e.ip_address) == "WAN" else "LAN",
            "location_status": _ip_location_status(e.ip_address),
            "online_status": "ONLINE" if p.get("status") in ("active", "idle") else "OFFLINE",
            "last_seen_at": e.last_seen_at.isoformat() if e.last_seen_at else None,
        })

    return {
        "total_employees": len(employees),
        "active_sessions": active_sessions_count,
        "online_active": online_count,
        "critical_alerts_24h": critical_alerts_24h,
        "recent_ip_audits": audits,
    }


@app.get("/status")
def status_grid(user: User = Depends(require_read), db: Session = Depends(get_db)):
    allowed = visible_employee_ids(user, db)
    q = db.query(Employee).filter(Employee.active == True)  # noqa: E712
    if allowed is not None:
        q = q.filter(Employee.employee_id.in_(allowed))
    employees = q.all()
    return get_all_presence([e.employee_id for e in employees])


@app.websocket("/ws/agent_heartbeat")
async def agent_heartbeat_ws(ws: WebSocket):
    """Agent connects and streams {employee_id, status, active_app} every 5-10s."""
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            update_presence(data["employee_id"], data["status"], data.get("active_app"))
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/console_status")
async def console_status_ws(ws: WebSocket):
    """Console connects here to receive instant presence pushes."""
    await console_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # console doesn't need to send anything; keep-alive
    except WebSocketDisconnect:
        console_manager.disconnect(ws)


# ---------- Remote sessions (IT/SuperAdmin only) ----------

@app.post("/remote/start/{employee_id}")
async def start_remote_session(employee_id: str, user: User = Depends(require_it), db: Session = Depends(get_db)):
    session = RemoteSession(employee_id=employee_id, started_by=user.username)
    db.add(session)
    db.commit()
    await remote_manager.command_to_agent(employee_id, {"type": "session_start", "session_id": session.id})
    log_audit(db, user, "remote_session_start", target_employee_id=employee_id, detail={"session_id": session.id})
    return {"session_id": session.id}


@app.post("/remote/end/{session_id}")
async def end_remote_session(session_id: int, user: User = Depends(require_it), db: Session = Depends(get_db)):
    session = db.query(RemoteSession).get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.ended_at = datetime.utcnow()
    db.commit()
    await remote_manager.command_to_agent(session.employee_id, {"type": "session_end", "session_id": session_id})
    duration = (session.ended_at - session.started_at).total_seconds()
    log_audit(db, user, "remote_session_end", target_employee_id=session.employee_id,
              detail={"session_id": session_id, "duration_seconds": duration})
    return {"status": "ended", "duration_seconds": duration}


@app.post("/remote/blank/{employee_id}")
async def set_blank_screen(employee_id: str, enabled: bool, color: str = "black",
                            user: User = Depends(require_it), db: Session = Depends(get_db)):
    await remote_manager.command_to_agent(employee_id, {"type": "blank_screen", "enabled": enabled, "color": color})
    log_audit(db, user, "remote_blank_screen", target_employee_id=employee_id,
              detail={"enabled": enabled, "color": color})
    return {"status": "sent"}


@app.websocket("/ws/remote/agent/{employee_id}")
async def remote_agent_ws(ws: WebSocket, employee_id: str):
    """Agent side of a remote session: sends JPEG frames, receives input/blank commands."""
    await remote_manager.register_agent(employee_id, ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await remote_manager.frame_to_console(employee_id, msg["bytes"])
    except Exception:
        pass
    finally:
        remote_manager.unregister_agent(employee_id)


@app.websocket("/ws/remote/console/{employee_id}")
async def remote_console_ws(ws: WebSocket, employee_id: str, token: str = ""):
    """
    Console side: receives frames, sends input/blank commands to the agent.
    Requires an IT/SuperAdmin JWT as a ?token= query param -- websocket
    handshakes here don't reliably carry an Authorization header the way
    plain HTTP requests do, so the token travels in the query string
    instead. Anyone without a valid IT-role token is rejected before the
    connection is accepted, so this relay can't be reached by a Manager/
    Viewer token or an unauthenticated client.
    """
    db = next(get_db())
    try:
        payload = decode_token(token)
        user = db.query(User).filter(User.username == payload.get("sub"), User.active == True).first()  # noqa: E712
        if not user or user.role not in (Role.SUPERADMIN, Role.ITSTAFF):
            await ws.close(code=4403)
            return
    except HTTPException:
        await ws.close(code=4401)
        return
    finally:
        db.close()

    await remote_manager.register_console(employee_id, ws)
    try:
        while True:
            command = await ws.receive_json()  # {"type": "mouse"/"key", ...}
            await remote_manager.command_to_agent(employee_id, command)
    except Exception:
        pass
    finally:
        remote_manager.unregister_console(employee_id)


# ---------- Alerts ----------

@app.get("/alerts/rules")
def list_alert_rules(user: User = Depends(require_it), db: Session = Depends(get_db)):
    rules = db.query(AlertRule).all()
    return [{"id": r.id, "name": r.name, "rule_type": r.rule_type, "params": r.params,
             "notify_via": r.notify_via, "notify_target": r.notify_target, "enabled": r.enabled} for r in rules]


@app.post("/alerts/rules")
def create_alert_rule(name: str = Form(...), rule_type: str = Form(...), params: str = Form(...),
                       notify_via: str = Form(...), notify_target: str = Form(...),
                       user: User = Depends(require_it), db: Session = Depends(get_db)):
    import json as _json
    rule = AlertRule(name=name, rule_type=rule_type, params=_json.loads(params),
                      notify_via=notify_via, notify_target=notify_target, created_by=user.username)
    db.add(rule)
    db.commit()
    return {"id": rule.id}


@app.delete("/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int, user: User = Depends(require_it), db: Session = Depends(get_db)):
    rule = db.query(AlertRule).get(rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    return {"status": "deleted"}


@app.get("/alerts/events")
def list_alert_events(user: User = Depends(require_read), db: Session = Depends(get_db)):
    allowed = visible_employee_ids(user, db)
    q = db.query(AlertEvent).order_by(AlertEvent.fired_at.desc())
    if allowed is not None:
        q = q.filter(AlertEvent.employee_id.in_(allowed))
    events = q.limit(100).all()
    return [{"id": e.id, "employee_id": e.employee_id, "message": e.message,
             "fired_at": e.fired_at.isoformat(), "acknowledged": e.acknowledged} for e in events]


# ---------- User & role management (SuperAdmin only) ----------

@app.post("/admin/users")
def create_user(username: str = Form(...), password: str = Form(...), role: str = Form(...),
                 display_name: str = Form(...), managed_employee_ids: str = Form("[]"),
                 user: User = Depends(require_admin), db: Session = Depends(get_db)):
    import json as _json
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    new_user = User(username=username, password_hash=hash_password(password), role=Role(role),
                     display_name=display_name, managed_employee_ids=_json.loads(managed_employee_ids))
    db.add(new_user)
    db.commit()
    log_audit(db, user, "create_user", detail={"created_username": username, "role": role})
    return {"id": new_user.id}


@app.get("/admin/users")
def list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role.value,
             "display_name": u.display_name, "active": u.active} for u in users]


@app.get("/admin/employees")
def list_employees(user: User = Depends(require_it), db: Session = Depends(get_db)):
    """
    Employees appear here automatically the moment their agent's consent
    dialog is accepted -- no manual registration step. This just lets
    IT see what's shown up and rename the display name if they want
    something friendlier than the raw employee_id/hostname.
    """
    employees = db.query(Employee).order_by(Employee.created_at.desc()).all()
    return [{
        "employee_id": e.employee_id, "display_name": e.display_name,
        "hostname": e.hostname, "ip_address": e.ip_address,
        "active": e.active, "created_at": e.created_at.isoformat(),
        "last_seen_at": e.last_seen_at.isoformat() if e.last_seen_at else None,
    } for e in employees]


@app.patch("/admin/employees/{employee_id}")
def rename_employee(employee_id: str, display_name: str = Form(...),
                     user: User = Depends(require_it), db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found -- has their agent run yet?")
    emp.display_name = display_name
    db.commit()
    log_audit(db, user, "rename_employee", target_employee_id=employee_id, detail={"display_name": display_name})
    return {"status": "ok"}


# ---------- Audit log ----------

@app.get("/audit")
def query_audit_log(user: User = Depends(require_admin), db: Session = Depends(get_db),
                     employee_id: Optional[str] = None, limit: int = 200):
    q = db.query(AuditLog).order_by(AuditLog.id.desc())
    if employee_id:
        q = q.filter(AuditLog.target_employee_id == employee_id)
    rows = q.limit(limit).all()
    return [{"actor": r.actor_username, "role": r.actor_role, "action": r.action,
             "target_employee_id": r.target_employee_id, "detail": r.detail, "at": r.at.isoformat()} for r in rows]


@app.get("/public/overview")
def get_public_overview(db: Session = Depends(get_db)):
    employees = db.query(Employee).order_by(Employee.last_seen_at.desc().nullslast()).all()
    presences = get_all_presence([e.employee_id for e in employees])
    online_count = sum(1 for p in presences if p.get("status") in ("active", "idle"))
    active_sessions_count = db.query(RemoteSession).filter(RemoteSession.ended_at.is_(None)).count()
    since = datetime.utcnow() - timedelta(hours=24)
    critical_alerts_24h = db.query(ActivityEvent).filter(
        ActivityEvent.severity == "critical", ActivityEvent.occurred_at >= since
    ).count()

    audits = []
    for e in employees[:10]:
        p = get_presence(e.employee_id)
        audits.append({
            "employee_id": e.employee_id, "display_name": e.display_name,
            "ip_address": e.ip_address, "ip_type": "WAN" if _ip_location_status(e.ip_address) == "WAN" else "LAN",
            "location_status": _ip_location_status(e.ip_address),
            "online_status": "ONLINE" if p.get("status") in ("active", "idle") else "OFFLINE",
            "last_seen_at": e.last_seen_at.isoformat() if e.last_seen_at else None,
        })

    return {
        "total_employees": len(employees),
        "active_sessions": active_sessions_count,
        "online_active": online_count,
        "critical_alerts_24h": critical_alerts_24h,
        "recent_ip_audits": audits,
    }


@app.get("/public/status")
def get_public_status(db: Session = Depends(get_db)):
    employees = db.query(Employee).filter(Employee.active == True).all()
    return get_all_presence([e.employee_id for e in employees])


@app.get("/public/employees")
def list_public_employees(db: Session = Depends(get_db)):
    employees = db.query(Employee).order_by(Employee.created_at.desc()).all()
    return [{
        "employee_id": e.employee_id,
        "display_name": e.display_name,
        "hostname": e.hostname,
        "ip_address": e.ip_address,
        "active": e.active,
        "created_at": e.created_at.isoformat(),
        "last_seen_at": e.last_seen_at.isoformat() if e.last_seen_at else None,
    } for e in employees]


@app.patch("/public/employees/{employee_id}")
def rename_public_employee(employee_id: str, display_name: str = Form(...), db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.employee_id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    emp.display_name = display_name
    db.commit()
    return {"status": "ok"}


@app.get("/public/screenshots/{employee_id}")
def list_public_screenshots(employee_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(Screenshot)
        .filter(Screenshot.employee_id == employee_id)
        .order_by(Screenshot.id.desc())
        .all()
    )
    return [{"id": r.id, "captured_at": r.captured_at.isoformat(), "stored_path": r.stored_path,
              "uploaded_at": r.uploaded_at.isoformat()} for r in rows]


@app.get("/public/screenshots/{employee_id}/{screenshot_id}/image")
def get_public_screenshot_image(employee_id: str, screenshot_id: int, db: Session = Depends(get_db)):
    row = (
        db.query(Screenshot)
        .filter(Screenshot.id == screenshot_id, Screenshot.employee_id == employee_id)
        .first()
    )
    if not row or not Path(row.stored_path).exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(row.stored_path, media_type="image/png")


@app.get("/public/screenshots/{employee_id}/latest")
def get_public_latest_screenshot(employee_id: str, db: Session = Depends(get_db)):
    row = (
        db.query(Screenshot)
        .filter(Screenshot.employee_id == employee_id)
        .order_by(Screenshot.id.desc())
        .first()
    )
    if not row or not Path(row.stored_path).exists():
        raise HTTPException(status_code=404, detail="No screenshot available")
    return FileResponse(row.stored_path, media_type="image/png")


@app.post("/public/remote/start/{employee_id}")
async def public_start_remote_session(employee_id: str, db: Session = Depends(get_db)):
    session = RemoteSession(employee_id=employee_id, started_by="AdminWeb")
    db.add(session)
    db.commit()
    ws = remote_manager.agent_sockets.get(employee_id)
    await remote_manager.command_to_agent(employee_id, {"type": "session_start", "session_id": session.id})
    return {"session_id": session.id, "connected": bool(ws)}


@app.post("/public/remote/end/{session_id}")
async def public_end_remote_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(RemoteSession).get(session_id)
    if session:
        session.ended_at = datetime.utcnow()
        db.commit()
        await remote_manager.command_to_agent(session.employee_id, {"type": "session_end", "session_id": session_id})
    return {"status": "ended"}


@app.post("/public/remote/blank/{employee_id}")
async def public_set_blank_screen(employee_id: str, enabled: bool = Form(...), color: str = Form("black"), db: Session = Depends(get_db)):
    ws = remote_manager.agent_sockets.get(employee_id)
    await remote_manager.command_to_agent(employee_id, {"type": "blank_screen", "enabled": enabled, "color": color})
    return {"status": "sent", "blanked": enabled, "connected": bool(ws)}


@app.websocket("/ws/public/remote/console/{employee_id}")
async def public_remote_console_ws(ws: WebSocket, employee_id: str):
    await remote_manager.register_console(employee_id, ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg and msg["text"]:
                import json as _json
                cmd = _json.loads(msg["text"])
                await remote_manager.command_to_agent(employee_id, cmd)
    except Exception:
        pass
    finally:
        remote_manager.unregister_console(employee_id)


@app.get("/public/activity/{employee_id}")
def list_public_activity(employee_id: str, event_type: Optional[str] = None, severity: Optional[str] = None,
                          limit: int = 200, db: Session = Depends(get_db)):
    query = db.query(ActivityEvent)
    if employee_id != "all":
        query = query.filter(ActivityEvent.employee_id == employee_id)
    if event_type and event_type != "All Types":
        query = query.filter(ActivityEvent.event_type == event_type)
    if severity and severity != "All Severities":
        query = query.filter(ActivityEvent.severity == severity)
    rows = query.order_by(ActivityEvent.id.desc()).limit(min(limit, 1000)).all()
    return [{"id": r.id, "employee_id": r.employee_id, "event_type": r.event_type,
              "severity": r.severity, "summary": r.summary, "detail": r.detail,
              "occurred_at": r.occurred_at.isoformat()} for r in rows]


@app.get("/public/alert_rules")
def list_public_alert_rules(db: Session = Depends(get_db)):
    rules = db.query(AlertRule).all()
    return [{"id": r.id, "name": r.name, "rule_type": r.rule_type, "params": r.params,
             "notify_via": r.notify_via, "notify_target": r.notify_target, "enabled": r.enabled} for r in rules]


@app.post("/public/alert_rules")
def create_public_alert_rule(name: str = Form(...), rule_type: str = Form(...), params: str = Form(...),
                             notify_via: str = Form(...), notify_target: str = Form(...),
                             db: Session = Depends(get_db)):
    import json as _json
    rule = AlertRule(name=name, rule_type=rule_type, params=_json.loads(params),
                      notify_via=notify_via, notify_target=notify_target, created_by="AdminWeb")
    db.add(rule)
    db.commit()
    return {"id": rule.id}


@app.delete("/public/alert_rules/{rule_id}")
def delete_public_alert_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(AlertRule).get(rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    return {"status": "deleted"}


@app.get("/public/alert_events")
def list_public_alert_events(limit: int = 150, db: Session = Depends(get_db)):
    events = db.query(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit).all()
    return [{"id": e.id, "rule_id": e.rule_id, "employee_id": e.employee_id,
             "message": e.message, "fired_at": e.fired_at.isoformat()} for e in events]


@app.get("/public/users")
def list_public_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role.value,
             "display_name": u.display_name, "active": u.active} for u in users]


@app.post("/public/users")
def create_public_user(username: str = Form(...), password: str = Form(...), role: str = Form(...),
                       display_name: str = Form(...), managed_employee_ids: str = Form("[]"),
                       db: Session = Depends(get_db)):
    import json as _json
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    new_user = User(username=username, password_hash=hash_password(password), role=Role(role),
                     display_name=display_name, managed_employee_ids=_json.loads(managed_employee_ids))
    db.add(new_user)
    db.commit()
    return {"id": new_user.id}


@app.get("/public/audit")
def query_public_audit_log(db: Session = Depends(get_db), limit: int = 200):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [{"actor": r.actor_username, "role": r.actor_role, "action": r.action,
             "target_employee_id": r.target_employee_id, "detail": r.detail, "at": r.at.isoformat()} for r in rows]


@app.get("/", response_class=HTMLResponse)
def index_web_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CYBER MONITOR // ENDPOINT COMMAND CONSOLE</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%23030805'/><rect width='90' height='90' x='5' y='5' rx='16' fill='%23000' stroke='%2300ff66' stroke-width='4'/><path d='M50 20 L75 35 L75 65 L50 80 L25 65 L25 35 Z' fill='none' stroke='%2300ff66' stroke-width='6'/></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #030805;
            --bg-grid: rgba(0, 255, 102, 0.03);
            --surface: rgba(8, 22, 14, 0.85);
            --surface-hover: rgba(14, 38, 24, 0.95);
            --neon-green: #00ff66;
            --neon-green-hover: #00e65c;
            --neon-green-glow: rgba(0, 255, 102, 0.35);
            --neon-cyan: #00f3ff;
            --warning: #ffcc00;
            --danger: #ff0055;
            --text: #f0fff4;
            --text-muted: #648a73;
            --border: rgba(0, 255, 102, 0.2);
            --border-glow: rgba(0, 255, 102, 0.4);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Outfit', sans-serif; }
        body {
            background-color: var(--bg);
            background-image: 
                radial-gradient(circle at 50% 0%, rgba(0, 255, 102, 0.12), transparent 75%),
                linear-gradient(var(--bg-grid) 1px, transparent 1px),
                linear-gradient(90deg, var(--bg-grid) 1px, transparent 1px);
            background-size: 100% 100%, 30px 30px, 30px 30px;
            color: var(--text);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        code, pre, .font-mono { font-family: 'JetBrains Mono', monospace; }
        
        header {
            background: rgba(4, 14, 9, 0.92);
            backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border);
            padding: 0.9rem 2.2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.6);
        }
        .logo-container { display: flex; align-items: center; gap: 14px; }
        .logo-icon {
            width: 42px; height: 42px;
            background: #000;
            border: 2px solid var(--neon-green);
            border-radius: 10px;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 0 15px var(--neon-green-glow);
        }
        .logo-title { font-size: 1.25rem; font-weight: 800; letter-spacing: 0.05em; color: var(--text); text-shadow: 0 0 10px rgba(0,255,102,0.3); }
        .logo-subtitle { font-size: 0.72rem; color: var(--neon-green); font-family: 'JetBrains Mono', monospace; letter-spacing: 0.15em; }
        .header-actions { display: flex; align-items: center; gap: 16px; }
        .status-pill {
            display: flex; align-items: center; gap: 10px;
            background: rgba(0, 255, 102, 0.08); color: var(--neon-green);
            border: 1px solid var(--border); padding: 6px 14px;
            border-radius: 20px; font-size: 0.8rem; font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
            box-shadow: 0 0 10px rgba(0,255,102,0.15);
        }
        .dot { width: 9px; height: 9px; background-color: var(--neon-green); border-radius: 50%; box-shadow: 0 0 10px var(--neon-green); animation: pulse 1.8s infinite; }
        @keyframes pulse { 0% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.3); } 100% { opacity: 1; transform: scale(1); } }
        
        .btn {
            background: rgba(0, 255, 102, 0.12); color: var(--neon-green);
            padding: 8px 16px; border-radius: 8px; font-weight: 600; font-size: 0.85rem;
            border: 1px solid var(--neon-green); cursor: pointer; transition: all 0.25s ease;
            display: inline-flex; align-items: center; gap: 8px; text-shadow: 0 0 8px rgba(0,255,102,0.3);
            box-shadow: 0 0 12px rgba(0, 255, 102, 0.15);
        }
        .btn:hover { background: var(--neon-green); color: #000; box-shadow: 0 0 20px var(--neon-green); text-shadow: none; }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text-muted); text-shadow: none; box-shadow: none; }
        .btn-outline:hover { background: var(--surface-hover); color: var(--neon-green); border-color: var(--neon-green); }
        .btn-danger { background: rgba(255, 0, 85, 0.15); color: var(--danger); border-color: var(--danger); text-shadow: 0 0 8px rgba(255,0,85,0.4); }
        .btn-danger:hover { background: var(--danger); color: #fff; box-shadow: 0 0 20px var(--danger); }

        /* Navigation Bar Tabs */
        nav.tab-bar {
            background-color: rgba(6, 18, 12, 0.95);
            border-bottom: 1px solid var(--border);
            display: flex; gap: 4px; padding: 0 1.8rem; overflow-x: auto;
        }
        .tab-btn {
            background: none; border: none; color: var(--text-muted);
            padding: 13px 20px; font-weight: 600; font-size: 0.875rem;
            cursor: pointer; border-bottom: 3px solid transparent;
            transition: all 0.2s ease; white-space: nowrap;
            display: flex; align-items: center; gap: 9px;
        }
        .tab-btn:hover { color: var(--text); background: rgba(0,255,102,0.04); }
        .tab-btn.active { color: var(--neon-green); border-bottom-color: var(--neon-green); background: rgba(0, 255, 102, 0.08); text-shadow: 0 0 10px rgba(0,255,102,0.4); }

        main { flex: 1; padding: 1.8rem 2.2rem; max-width: 1550px; width: 100%; margin: 0 auto; }
        
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.25s ease-in; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

        /* Cyber Cards & UI Components */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px; padding: 1.4rem; margin-bottom: 1.8rem;
            backdrop-filter: blur(12px);
            box-shadow: 0 8px 30px rgba(0,0,0,0.5);
            transition: border-color 0.3s ease;
        }
        .card:hover { border-color: var(--border-glow); }
        .card-title { font-size: 1.15rem; font-weight: 700; margin-bottom: 1.2rem; display: flex; justify-content: space-between; align-items: center; color: var(--neon-green); letter-spacing: 0.03em; }
        
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1.4rem; margin-bottom: 1.8rem; }
        .stat-card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 14px; padding: 1.3rem; display: flex; flex-direction: column; gap: 8px;
            position: relative; overflow: hidden;
        }
        .stat-card::after { content: ''; position: absolute; top: 0; right: 0; width: 4px; height: 100%; background: var(--neon-green); box-shadow: 0 0 10px var(--neon-green); }
        .stat-label { font-size: 0.8rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; }
        .stat-value { font-size: 2.1rem; font-weight: 800; color: #fff; font-family: 'JetBrains Mono', monospace; text-shadow: 0 0 12px rgba(255,255,255,0.2); }

        table.data-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.875rem; table-layout: fixed; }
        table.data-table th, table.data-table td { padding: 12px 16px; border-bottom: 1px solid rgba(0,255,102,0.08); word-break: break-all; overflow-wrap: anywhere; }
        table.data-table th { background: rgba(0,0,0,0.5); color: var(--neon-green); font-weight: 700; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.08em; border-bottom: 1px solid var(--border); }
        table.data-table tr:hover { background: rgba(0,255,102,0.03); }
        .summary-link { color: var(--neon-cyan); text-decoration: underline; word-break: break-all; overflow-wrap: anywhere; text-shadow: 0 0 8px rgba(0,243,255,0.3); }
        .summary-link:hover { color: #80f8ff; }

        .badge { padding: 4px 10px; border-radius: 6px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: inline-block; font-family: 'JetBrains Mono', monospace; }
        .badge-active, .badge-info { background: rgba(0, 255, 102, 0.15); color: var(--neon-green); border: 1px solid rgba(0, 255, 102, 0.3); box-shadow: 0 0 8px rgba(0,255,102,0.2); }
        .badge-idle, .badge-warn { background: rgba(255, 204, 0, 0.15); color: var(--warning); border: 1px solid rgba(255, 204, 0, 0.3); box-shadow: 0 0 8px rgba(255,204,0,0.2); }
        .badge-offline, .badge-critical { background: rgba(255, 0, 85, 0.18); color: var(--danger); border: 1px solid rgba(255, 0, 85, 0.3); box-shadow: 0 0 8px rgba(255,0,85,0.3); }

        .cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1.4rem; }
        .employee-card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; box-shadow: 0 6px 20px rgba(0,0,0,0.4); }
        .card-header { padding: 1.1rem; display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px solid rgba(0,255,102,0.1); }
        .card-body { padding: 1.1rem; }
        .thumb-preview {
            width: 100%; height: 175px; background-color: #010403;
            border-radius: 8px; display: flex; align-items: center; justify-content: center;
            overflow: hidden; border: 1px solid var(--border); margin-bottom: 12px; cursor: pointer;
            transition: all 0.3s ease;
        }
        .thumb-preview:hover { border-color: var(--neon-green); box-shadow: 0 0 15px var(--neon-green-glow); }
        .thumb-preview img { width: 100%; height: 100%; object-fit: cover; }

        /* Form elements */
        .form-row { display: flex; gap: 14px; margin-bottom: 14px; flex-wrap: wrap; }
        .form-group { display: flex; flex-direction: column; gap: 5px; flex: 1; min-width: 190px; }
        .form-group label { font-size: 0.8rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .input, select {
            background: #040d08; border: 1px solid var(--border);
            color: var(--text); padding: 9px 14px; border-radius: 8px; font-size: 0.875rem; outline: none;
            font-family: 'JetBrains Mono', monospace; transition: all 0.2s ease;
        }
        .input:focus, select:focus { border-color: var(--neon-green); box-shadow: 0 0 12px var(--neon-green-glow); }

        /* Modal Lightbox */
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.88); backdrop-filter: blur(10px); z-index: 1000; justify-content: center; align-items: center; }
        .modal-content { background: var(--surface); border: 2px solid var(--neon-green); border-radius: 16px; padding: 1.5rem; max-width: 90vw; max-height: 90vh; display: flex; flex-direction: column; gap: 14px; box-shadow: 0 0 30px var(--neon-green-glow); }
        .modal-content img { max-width: 100%; max-height: 75vh; border-radius: 8px; object-fit: contain; }
    </style>
</head>
<body>

    <header>
        <div class="logo-container">
            <div class="logo-icon">
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#00ff66" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
            </div>
            <div>
                <div class="logo-title">CYBER MONITOR</div>
                <div class="logo-subtitle">// ENDPOINT COMMAND CONSOLE</div>
            </div>
        </div>
        <div class="header-actions">
            <div class="status-pill">
                <div class="dot"></div>
                SYSTEM ONLINE
            </div>
            <button class="btn btn-outline" onclick="refreshCurrentTab()">Refresh Data</button>
        </div>
    </header>

    <nav class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('overview')">Overview</button>
        <button class="tab-btn" onclick="switchTab('live-status')">Live Status</button>
        <button class="tab-btn" onclick="switchTab('employees')">Endpoints</button>
        <button class="tab-btn" onclick="switchTab('gallery')">Gallery</button>
        <button class="tab-btn" onclick="switchTab('live-view')">Live Wall</button>
        <button class="tab-btn" onclick="switchTab('remote')">Remote Desktop</button>
        <button class="tab-btn" onclick="switchTab('activity')">Activity Logs</button>
        <button class="tab-btn" onclick="switchTab('alerts')">Alerts & Rules</button>
        <button class="tab-btn" onclick="switchTab('users')">Console Users</button>
        <button class="tab-btn" onclick="switchTab('audit')">Audit Logs</button>
    </nav>

    <main>
        <!-- 1. Overview -->
        <div id="tab-overview" class="tab-content active">
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Total Endpoints</div>
                    <div class="stat-value" id="ov-total">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Active Online</div>
                    <div class="stat-value" id="ov-online" style="color: var(--neon-green);">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Active Remote Control Sessions</div>
                    <div class="stat-value" id="ov-remote" style="color: var(--neon-cyan);">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Critical Alerts (24h)</div>
                    <div class="stat-value" id="ov-alerts" style="color: var(--danger);">0</div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Endpoint Network & IP Audits</div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Endpoint Name</th>
                                <th>IP Address</th>
                                <th>IP Type</th>
                                <th>Network Location</th>
                                <th>Presence Status</th>
                            </tr>
                        </thead>
                        <tbody id="ov-audits-table">
                            <tr><td colspan="5" style="text-align:center; color: var(--text-muted);">Loading telemetry...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 2. Live Status -->
        <div id="tab-live-status" class="tab-content">
            <div class="card">
                <div class="card-title">Real-Time Endpoint Status</div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Endpoint ID</th>
                                <th>Status</th>
                                <th>Active Application</th>
                                <th>Last Active Time</th>
                            </tr>
                        </thead>
                        <tbody id="status-table">
                            <tr><td colspan="4" style="text-align:center; color: var(--text-muted);">Loading live status...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 3. Employees -->
        <div id="tab-employees" class="tab-content">
            <div class="card">
                <div class="card-title">Registered Endpoints</div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Display Name</th>
                                <th>Hostname</th>
                                <th>IP Address</th>
                                <th>Registered Date</th>
                                <th>Last Seen</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="employees-table">
                            <tr><td colspan="7" style="text-align:center; color: var(--text-muted);">Loading endpoints...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 4. Screenshot Gallery -->
        <div id="tab-gallery" class="tab-content">
            <div class="card">
                <div class="card-title">
                    <span>Screenshot Gallery</span>
                    <div style="display:flex; gap:10px; align-items:center;">
                        <select id="gallery-emp-select" onchange="loadGallery()"></select>
                    </div>
                </div>
                <div id="gallery-grid" class="cards-grid"></div>
            </div>
        </div>

        <!-- 5. Live View Wall -->
        <div id="tab-live-view" class="tab-content">
            <div class="card">
                <div class="card-title">Live Monitoring Wall</div>
                <div id="live-wall-grid" class="cards-grid"></div>
            </div>
        </div>

        <!-- 6. Remote Desktop -->
        <div id="tab-remote" class="tab-content">
            <div class="card">
                <div class="card-title">Real-Time Remote Desktop Control</div>
                <div class="form-row" style="align-items:center; margin-bottom:1rem;">
                    <div class="form-group">
                        <label>Select Endpoint Machine</label>
                        <select id="remote-emp-select"></select>
                    </div>
                    <div style="display:flex; gap:10px; align-self:flex-end;">
                        <button id="btn-start-remote" class="btn" onclick="startRemoteSession()">Start Remote Control</button>
                        <button id="btn-stop-remote" class="btn btn-danger" onclick="stopRemoteSession()" style="display:none;">Stop Remote Session</button>
                        <button class="btn btn-outline" onclick="toggleBlankScreen(true)" style="border-color:#ffcc00; color:#ffcc00;">Blank Target Screen</button>
                        <button class="btn btn-outline" onclick="toggleBlankScreen(false)">Restore Screen</button>
                    </div>
                </div>

                <div id="remote-screen-container" style="background:#000; border:1px solid var(--border); border-radius:10px; min-height:450px; display:flex; align-items:center; justify-content:center; padding:1.5rem; text-align:center; color:var(--text-muted);">
                    Select an employee above and click "Start Remote Control" to view and interactively control the employee screen.
                </div>
            </div>
        </div>

        <!-- 7. Activity Monitor -->
        <div id="tab-activity" class="tab-content">
            <div class="card">
                <div class="card-title">
                    <span>Activity & Forensic Event Logs</span>
                    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                        <select id="act-emp-select" onchange="loadActivity()"></select>
                        <select id="act-type-select" onchange="loadActivity()">
                            <option value="All Types">All Types</option>
                            <option value="BROWSER">BROWSER</option>
                            <option value="APP">APP</option>
                            <option value="USB">USB</option>
                            <option value="FILE">FILE</option>
                        </select>
                        <select id="act-sev-select" onchange="loadActivity()">
                            <option value="All Severities">All Severities</option>
                            <option value="info">info</option>
                            <option value="warn">warn</option>
                            <option value="critical">critical</option>
                        </select>
                    </div>
                </div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th style="width: 170px;">Local Time</th>
                                <th style="width: 140px;">Endpoint</th>
                                <th style="width: 110px;">Type</th>
                                <th style="width: 110px;">Severity</th>
                                <th>Event Details / Summary</th>
                            </tr>
                        </thead>
                        <tbody id="activity-table">
                            <tr><td colspan="5" style="text-align:center; color: var(--text-muted);">Loading activity...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 8. Alert Rules & Events -->
        <div id="tab-alerts" class="tab-content">
            <div class="card" style="border-color: rgba(255, 0, 85, 0.3);">
                <div class="card-title" style="color: var(--danger);">Live Fired Alert Events</div>
                <div style="overflow-x: auto; margin-bottom: 1.5rem;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th style="width: 170px;">Fired Local Time</th>
                                <th style="width: 140px;">Endpoint ID</th>
                                <th>Alert Message & Details</th>
                            </tr>
                        </thead>
                        <tbody id="alert-events-table">
                            <tr><td colspan="3" style="text-align:center; color: var(--text-muted);">Loading alert events...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Configured Alert Rules</div>
                <div class="form-row" style="margin-bottom:1rem;">
                    <div class="form-group"><label>Rule Name</label><input id="rule-name" class="input" placeholder="e.g. WhatsApp / Notepad Alert" /></div>
                    <div class="form-group">
                        <label>Rule Type</label>
                        <select id="rule-type" class="input">
                            <option value="blacklisted_app">Blacklisted Application</option>
                            <option value="idle_minutes">Idle Minutes Threshold</option>
                            <option value="after_hours">After Hours Activity</option>
                        </select>
                    </div>
                    <div class="form-group"><label>Params (JSON)</label><input id="rule-params" class="input" value='{"app_name": "whatsapp"}' /></div>
                    <div class="form-group"><label>Target (Webhook/Email)</label><input id="rule-target" class="input" value="http://localhost:8000/webhook" /></div>
                    <div style="align-self:flex-end;"><button class="btn" onclick="createAlertRule()">Create Rule</button></div>
                </div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Type</th>
                                <th>Parameters</th>
                                <th>Notify Via</th>
                                <th>Target</th>
                                <th>Status</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="alerts-table">
                            <tr><td colspan="7" style="text-align:center; color: var(--text-muted);">Loading alert rules...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 9. Users & Roles -->
        <div id="tab-users" class="tab-content">
            <div class="card">
                <div class="card-title">System Console Users</div>
                <div class="form-row" style="margin-bottom:1rem;">
                    <div class="form-group"><label>Username</label><input id="user-username" class="input" placeholder="john_doe" /></div>
                    <div class="form-group"><label>Password</label><input id="user-password" type="password" class="input" placeholder="••••••••" /></div>
                    <div class="form-group"><label>Display Name</label><input id="user-display" class="input" placeholder="John Doe" /></div>
                    <div class="form-group">
                        <label>Role</label>
                        <select id="user-role" class="input">
                            <option value="IT_ADMIN">IT Admin</option>
                            <option value="MANAGER">Manager</option>
                            <option value="SUPERADMIN">Super Admin</option>
                        </select>
                    </div>
                    <div style="align-self:flex-end;"><button class="btn" onclick="createUser()">Add User</button></div>
                </div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Username</th>
                                <th>Display Name</th>
                                <th>Role</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="users-table">
                            <tr><td colspan="5" style="text-align:center; color: var(--text-muted);">Loading users...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 10. Audit Log -->
        <div id="tab-audit" class="tab-content">
            <div class="card">
                <div class="card-title">Admin Audit Logs</div>
                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th style="width: 170px;">Local Time</th>
                                <th>Actor Username</th>
                                <th>Actor Role</th>
                                <th>Action</th>
                                <th>Target Employee</th>
                                <th>Details</th>
                            </tr>
                        </thead>
                        <tbody id="audit-table">
                            <tr><td colspan="6" style="text-align:center; color: var(--text-muted);">Loading audit log...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <!-- Modal Lightbox -->
    <div id="lightbox" class="modal" onclick="closeLightbox(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div id="lightbox-caption" style="font-weight:700; color:var(--neon-green);">Screenshot Preview</div>
                <button class="btn btn-outline" onclick="document.getElementById('lightbox').style.display='none'">✕ Close</button>
            </div>
            <img id="lightbox-img" src="" alt="Screenshot" />
        </div>
    </div>

    <script>
        let activeTab = 'overview';

        // Local Time Formatter Function
        function formatLocalTime(isoStr) {
            if (!isoStr) return '—';
            try {
                const dt = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + (isoStr.includes('T') && !isoStr.includes('+') ? 'Z' : ''));
                if (isNaN(dt.getTime())) return isoStr;
                return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }) +
                       ' (' + dt.toLocaleDateString() + ')';
            } catch (e) {
                return isoStr;
            }
        }

        function switchTab(tabId) {
            activeTab = tabId;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            
            const targetBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.innerText.toLowerCase().includes(tabId.replace('-', ' ')));
            if (targetBtn) targetBtn.classList.add('active');
            
            const targetTab = document.getElementById('tab-' + tabId);
            if (targetTab) targetTab.classList.add('active');

            refreshCurrentTab();
        }

        function refreshCurrentTab() {
            if (activeTab === 'overview') loadOverview();
            if (activeTab === 'live-status') loadLiveStatus();
            if (activeTab === 'employees') loadEmployees();
            if (activeTab === 'gallery') loadGallery();
            if (activeTab === 'live-view') loadLiveView();
            if (activeTab === 'activity') loadActivity();
            if (activeTab === 'alerts') loadAlerts();
            if (activeTab === 'users') loadUsers();
            if (activeTab === 'audit') loadAudit();
        }

        async function populateEmployeeDropdowns() {
            const res = await fetch('/public/employees');
            const emps = await res.json();
            const selects = ['gallery-emp-select', 'remote-emp-select', 'act-emp-select'];
            selects.forEach(id => {
                const sel = document.getElementById(id);
                if (!sel) return;
                const curVal = sel.value;
                sel.innerHTML = id === 'act-emp-select' ? '<option value="all">All Employees</option>' : '';
                emps.forEach(e => {
                    const opt = document.createElement('option');
                    opt.value = e.employee_id;
                    opt.innerText = `${e.display_name} (${e.employee_id})`;
                    sel.appendChild(opt);
                });
                if (curVal) sel.value = curVal;
            });
        }

        // 1. Overview
        async function loadOverview() {
            const res = await fetch('/public/overview');
            const data = await res.json();
            document.getElementById('ov-total').innerText = data.total_employees;
            document.getElementById('ov-online').innerText = data.online_active;
            document.getElementById('ov-remote').innerText = data.active_sessions;
            document.getElementById('ov-alerts').innerText = data.critical_alerts_24h;

            const tbody = document.getElementById('ov-audits-table');
            if (data.recent_ip_audits.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-muted);">No IP audits logged yet</td></tr>';
                return;
            }
            tbody.innerHTML = data.recent_ip_audits.map(a => `
                <tr>
                    <td><strong>${a.display_name}</strong></td>
                    <td><code>${a.ip_address || '—'}</code></td>
                    <td>${a.ip_type}</td>
                    <td><span class="badge ${a.location_status === 'OFFICE' ? 'badge-active' : 'badge-warn'}">${a.location_status}</span></td>
                    <td><span class="badge ${a.online_status === 'ONLINE' ? 'badge-active' : 'badge-offline'}">${a.online_status}</span></td>
                </tr>
            `).join('');
        }

        // 2. Live Status
        async function loadLiveStatus() {
            const res = await fetch('/public/status');
            const data = await res.json();
            const tbody = document.getElementById('status-table');
            if (!data || data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">No active agents reporting</td></tr>';
                return;
            }
            tbody.innerHTML = data.map(s => `
                <tr>
                    <td><code>${s.employee_id}</code></td>
                    <td><span class="badge ${s.status === 'active' ? 'badge-active' : s.status === 'idle' ? 'badge-idle' : 'badge-offline'}">${s.status}</span></td>
                    <td>${s.active_app || '—'}</td>
                    <td>${s.last_seen ? new Date(s.last_seen * 1000).toLocaleTimeString() : 'Never'}</td>
                </tr>
            `).join('');
        }

        // 3. Employees
        async function loadEmployees() {
            const res = await fetch('/public/employees');
            const emps = await res.json();
            const tbody = document.getElementById('employees-table');
            if (emps.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color: var(--text-muted);">No employees registered yet</td></tr>';
                return;
            }
            tbody.innerHTML = emps.map(e => `
                <tr>
                    <td><code>${e.employee_id}</code></td>
                    <td><strong>${e.display_name}</strong></td>
                    <td>${e.hostname || '—'}</td>
                    <td><code>${e.ip_address || '—'}</code></td>
                    <td>${formatLocalTime(e.created_at)}</td>
                    <td>${formatLocalTime(e.last_seen_at)}</td>
                    <td><button class="btn btn-outline" onclick="renameEmployee('${e.employee_id}')">Rename</button></td>
                </tr>
            `).join('');
            populateEmployeeDropdowns();
        }

        async function renameEmployee(id) {
            const newName = prompt('Enter new display name for employee ' + id + ':');
            if (!newName) return;
            const body = new FormData();
            body.append('display_name', newName);
            await fetch('/public/employees/' + id, { method: 'PATCH', body });
            loadEmployees();
        }

        // 4. Screenshot Gallery
        async function loadGallery() {
            await populateEmployeeDropdowns();
            const empId = document.getElementById('gallery-emp-select').value;
            if (!empId) return;
            const res = await fetch('/public/screenshots/' + empId);
            const shots = await res.json();
            const grid = document.getElementById('gallery-grid');
            if (shots.length === 0) {
                grid.innerHTML = '<div style="grid-column: 1/-1; text-align:center; color: var(--text-muted); padding:3rem;">No screenshots uploaded for this employee yet.</div>';
                return;
            }
            grid.innerHTML = shots.map(s => `
                <div class="employee-card">
                    <div class="card-body">
                        <div class="thumb-preview" onclick="openLightbox('/public/screenshots/${empId}/${s.id}/image', '${s.captured_at}')">
                            <img src="/public/screenshots/${empId}/${s.id}/image" />
                        </div>
                        <div style="font-size:0.8rem; color:var(--neon-green); font-family:'JetBrains Mono';">Captured: ${formatLocalTime(s.captured_at)}</div>
                    </div>
                </div>
            `).join('');
        }

        // 5. Live View Wall
        async function loadLiveView() {
            const [empRes, statusRes] = await Promise.all([
                fetch('/public/employees'),
                fetch('/public/status')
            ]);
            const emps = await empRes.json();
            const statuses = await statusRes.json();

            const statusMap = {};
            if (Array.isArray(statuses)) {
                statuses.forEach(s => {
                    statusMap[s.employee_id] = s.status;
                });
            }

            const grid = document.getElementById('live-wall-grid');
            if (emps.length === 0) {
                grid.innerHTML = '<div style="grid-column: 1/-1; text-align:center; color: var(--text-muted); padding:3rem;">No live employees available.</div>';
                return;
            }

            grid.innerHTML = emps.map(e => {
                const status = statusMap[e.employee_id] || 'offline';
                const isOnline = (status === 'active' || status === 'idle');

                const badgeHtml = isOnline 
                    ? `<span class="badge badge-active">LIVE</span>`
                    : `<span class="badge badge-offline" style="background: rgba(255, 51, 85, 0.15); color: #ff3355; border: 1px solid rgba(255, 51, 85, 0.3);">OFFLINE</span>`;

                const screenContentHtml = isOnline
                    ? `<div class="thumb-preview" onclick="openLightbox('/public/screenshots/${e.employee_id}/latest?t=${Date.now()}', '${e.display_name} Live Screen')">
                           <img src="/public/screenshots/${e.employee_id}/latest?t=${Date.now()}" onerror="this.onerror=null; this.parentElement.innerHTML='<span style=\\'padding: 20px; text-align: center; font-size: 0.8rem; color: var(--text-muted);\\'>No live thumbnail yet</span>';" />
                       </div>`
                    : `<div style="width:100%; height:200px; background:#000000; border-radius:8px; border:1px solid rgba(255, 51, 85, 0.3); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; box-shadow: inset 0 0 20px rgba(0,0,0,0.9);">
                           <div style="width:12px; height:12px; background:#ff3355; border-radius:50%; box-shadow: 0 0 12px #ff3355;"></div>
                           <div style="font-weight:700; color:#ff3355; font-size:0.85rem; letter-spacing:1.5px; font-family:'JetBrains Mono';">SIGNAL LOST // OFFLINE</div>
                           <div style="font-size:0.75rem; color:var(--text-muted);">Screen feed paused until agent reconnects</div>
                       </div>`;

                return `
                    <div class="employee-card" style="${!isOnline ? 'opacity: 0.85; border-color: rgba(255, 51, 85, 0.3);' : ''}">
                        <div class="card-header">
                            <div>
                                <div style="font-weight:700; color:var(--text);">${e.display_name}</div>
                                <div style="font-size:0.75rem; color:var(--text-muted); font-family:'JetBrains Mono';">${e.employee_id}</div>
                            </div>
                            ${badgeHtml}
                        </div>
                        <div class="card-body">
                            ${screenContentHtml}
                        </div>
                    </div>
                `;
            }).join('');
        }

        // 6. Remote Control
        let remoteSessionTimer = null;
        let remoteWs = null;
        let activeRemoteSessionId = null;

        async function toggleBlankScreen(enable) {
            const empId = document.getElementById('remote-emp-select').value;
            if (!empId) return alert('Select an employee first!');
            const body = new FormData();
            body.append('enabled', enable ? 'true' : 'false');
            body.append('color', 'black');
            const res = await fetch('/public/remote/blank/' + empId, { method: 'POST', body });
            const data = await res.json();
            if (data.connected === false) {
                alert(`[System Warning] Endpoint (${empId}) is running an older agent version and is NOT connected to the live remote channel.\n\nPlease update employee_agent.exe on ${empId} to activate screen blanking.`);
            } else {
                alert(enable ? `[Screen Blanked] Target screen (${empId}) is now set to Black Screen.` : `[Screen Restored] Target screen (${empId}) restored to normal.`);
            }
        }

        let lastMoveTime = 0;
        function handleMouseMove(evt) {
            const now = Date.now();
            if (now - lastMoveTime < 35) return;
            lastMoveTime = now;
            sendRemoteInput('move', evt);
        }

        function sendRemoteInput(action, evt) {
            if (!remoteWs || remoteWs.readyState !== WebSocket.OPEN) return;
            const img = document.getElementById('remote-stream-img');
            if (!img) return;

            const rect = img.getBoundingClientRect();
            const natW = img.naturalWidth || 1920;
            const natH = img.naturalHeight || 1080;

            const containerWidth = rect.width;
            const containerHeight = rect.height;
            const naturalRatio = natW / natH;
            const containerRatio = containerWidth / containerHeight;

            let renderWidth, renderHeight, offsetX, offsetY;
            if (containerRatio > naturalRatio) {
                renderHeight = containerHeight;
                renderWidth = containerHeight * naturalRatio;
                offsetX = (containerWidth - renderWidth) / 2;
                offsetY = 0;
            } else {
                renderWidth = containerWidth;
                renderHeight = containerWidth / naturalRatio;
                offsetX = 0;
                offsetY = (containerHeight - renderHeight) / 2;
            }

            const clickX = evt.clientX - rect.left - offsetX;
            const clickY = evt.clientY - rect.top - offsetY;

            if (clickX < 0 || clickX > renderWidth || clickY < 0 || clickY > renderHeight) return;

            const xPct = clickX / renderWidth;
            const yPct = clickY / renderHeight;

            const x = Math.round(xPct * natW);
            const y = Math.round(yPct * natH);

            const payload = JSON.stringify({
                type: "mouse",
                action: action,
                x: x,
                y: y
            });
            remoteWs.send(payload);
        }

        let keyboardListenersBound = false;
        function setupRemoteKeyboard() {
            if (keyboardListenersBound) return;
            keyboardListenersBound = true;

            window.addEventListener('keydown', (evt) => {
                if (!remoteWs || remoteWs.readyState !== WebSocket.OPEN) return;
                const tag = document.activeElement ? document.activeElement.tagName : '';
                if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
                
                if ((evt.keyCode >= 112 && evt.keyCode <= 123) || evt.keyCode === 9 || evt.keyCode === 8) {
                    evt.preventDefault();
                }
                remoteWs.send(JSON.stringify({
                    type: "key",
                    vk: evt.keyCode,
                    action: "down"
                }));
            });

            window.addEventListener('keyup', (evt) => {
                if (!remoteWs || remoteWs.readyState !== WebSocket.OPEN) return;
                const tag = document.activeElement ? document.activeElement.tagName : '';
                if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

                remoteWs.send(JSON.stringify({
                    type: "key",
                    vk: evt.keyCode,
                    action: "up"
                }));
            });
        }

        async function startRemoteSession() {
            const empId = document.getElementById('remote-emp-select').value;
            if (!empId) return alert('Select an employee first!');

            setupRemoteKeyboard();

            let isConnected = false;
            try {
                const res = await fetch('/public/remote/start/' + empId, { method: 'POST' });
                const data = await res.json();
                activeRemoteSessionId = data.session_id;
                isConnected = data.connected;
            } catch (e) {
                console.log('Remote start trigger:', e);
            }

            const btnStart = document.getElementById('btn-start-remote');
            const btnStop = document.getElementById('btn-stop-remote');
            if (btnStart) btnStart.style.display = 'none';
            if (btnStop) btnStop.style.display = 'inline-flex';

            const container = document.getElementById('remote-screen-container');

            if (!isConnected) {
                container.innerHTML = `
                    <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; width:100%; min-height:450px; background:#000; border-radius:12px; border:1px solid rgba(255, 51, 85, 0.4); box-shadow: inset 0 0 30px rgba(0,0,0,0.95); padding:2rem; text-align:center;">
                        <div style="display:flex; align-items:center; justify-content:space-between; width:100%; margin-bottom:1rem;">
                            <div class="badge badge-offline" style="background: rgba(255, 51, 85, 0.15); color: #ff3355; border: 1px solid rgba(255, 51, 85, 0.3);">OFFLINE</div>
                            <div style="font-size:0.85rem; color:var(--danger); font-weight:700; font-family:'JetBrains Mono';">REMOTE SESSION STANDBY</div>
                        </div>
                        <div style="width:14px; height:14px; background:#ff3355; border-radius:50%; box-shadow: 0 0 14px #ff3355; animation: pulse 1.5s infinite;"></div>
                        <div style="font-weight:800; color:#ff3355; font-size:1.1rem; letter-spacing:1.5px; font-family:'JetBrains Mono';">TARGET ENDPOINT OFFLINE</div>
                        <div style="font-size:0.85rem; color:var(--text-muted); max-width:480px;">Target machine (${empId}) is currently offline or disconnected.<br>Live remote desktop stream cannot be established until target PC reconnects.</div>
                    </div>
                `;
                return;
            }

            container.innerHTML = `
                <div style="display:flex; flex-direction:column; align-items:center; gap:14px; width:100%;">
                    <div style="display:flex; align-items:center; justify-content:space-between; width:100%;">
                        <div class="badge badge-active">Live Tracking: ${empId}</div>
                        <div id="remote-live-status-indicator" style="font-size:0.85rem; color:var(--neon-green); font-weight:700; font-family:'JetBrains Mono';">● Live 20 FPS Remote Control & Keyboard Active</div>
                    </div>
                    <img id="remote-stream-img" 
                         src="/public/screenshots/${empId}/latest?t=${Date.now()}" 
                         onmousemove="handleMouseMove(event)"
                         onclick="sendRemoteInput('left_click', event)" 
                         ondblclick="sendRemoteInput('double_click', event)" 
                         oncontextmenu="event.preventDefault(); sendRemoteInput('right_click', event)"
                         style="max-width:100%; max-height:600px; border-radius:10px; border:2px solid var(--neon-green); box-shadow: 0 0 25px var(--neon-green-glow); object-fit:contain; cursor:crosshair;" 
                         title="Move cursor to track mouse live / Type on keyboard to control target PC" />
                </div>
            `;

            let lastFrameTime = Date.now();
            try {
                const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                remoteWs = new WebSocket(`${wsProto}//${location.host}/ws/public/remote/console/${empId}`);
                remoteWs.binaryType = 'arraybuffer';
                remoteWs.onmessage = (event) => {
                    if (event.data instanceof ArrayBuffer) {
                        lastFrameTime = Date.now();
                        const blob = new Blob([event.data], { type: 'image/jpeg' });
                        const url = URL.createObjectURL(blob);
                        const img = document.getElementById('remote-stream-img');
                        if (img) {
                            const oldUrl = img.src;
                            img.src = url;
                            if (oldUrl && oldUrl.startsWith('blob:')) {
                                URL.revokeObjectURL(oldUrl);
                            }
                        }
                    }
                };
            } catch (err) {
                console.log('WebSocket connection error:', err);
            }

            if (remoteSessionTimer) clearInterval(remoteSessionTimer);
            remoteSessionTimer = setInterval(() => {
                if (Date.now() - lastFrameTime > 3500) {
                    const indicator = document.getElementById('remote-live-status-indicator');
                    if (indicator) {
                        indicator.style.color = 'var(--danger)';
                        indicator.innerText = '● Connection Timed Out (Target Offline)';
                    }
                }
            }, 1000);
        }

        async function stopRemoteSession() {
            if (remoteSessionTimer) {
                clearInterval(remoteSessionTimer);
                remoteSessionTimer = null;
            }
            if (remoteWs) {
                remoteWs.close();
                remoteWs = null;
            }
            if (activeRemoteSessionId) {
                try {
                    await fetch('/public/remote/end/' + activeRemoteSessionId, { method: 'POST' });
                } catch (e) {}
                activeRemoteSessionId = null;
            }

            const btnStart = document.getElementById('btn-start-remote');
            const btnStop = document.getElementById('btn-stop-remote');
            if (btnStart) btnStart.style.display = 'inline-flex';
            if (btnStop) btnStop.style.display = 'none';

            const container = document.getElementById('remote-screen-container');
            container.innerHTML = 'Select an employee above and click "Start Remote Control" to view and interactively control the employee screen.';
        }

        // 7. Activity Monitor
        function formatSummaryWithLinks(summary) {
            if (!summary) return '—';
            const urlRegex = /(https?:\/\/[^\s]+)/g;
            return summary.replace(urlRegex, (url) => {
                return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="summary-link">${url}</a>`;
            });
        }

        async function loadActivity() {
            await populateEmployeeDropdowns();
            const empId = document.getElementById('act-emp-select').value || 'all';
            const type = document.getElementById('act-type-select').value;
            const sev = document.getElementById('act-sev-select').value;
            const res = await fetch(`/public/activity/${empId}?event_type=${type}&severity=${sev}`);
            const events = await res.json();
            const tbody = document.getElementById('activity-table');
            if (events.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-muted);">No activity events recorded</td></tr>';
                return;
            }
            tbody.innerHTML = events.map(ev => `
                <tr>
                    <td style="width: 170px; font-family:'JetBrains Mono'; color:var(--neon-green);">${formatLocalTime(ev.occurred_at)}</td>
                    <td style="width: 140px;"><code>${ev.employee_id}</code></td>
                    <td style="width: 110px;"><span class="badge badge-info">${ev.event_type}</span></td>
                    <td style="width: 110px;"><span class="badge ${ev.severity === 'critical' ? 'badge-critical' : ev.severity === 'warn' ? 'badge-warn' : 'badge-info'}">${ev.severity}</span></td>
                    <td style="word-break: break-all; overflow-wrap: anywhere;"><strong style="font-weight:500;">${formatSummaryWithLinks(ev.summary)}</strong></td>
                </tr>
            `).join('');
        }

        // 8. Alert Rules & Events
        async function loadAlerts() {
            // Load Fired Alert Events
            try {
                const evRes = await fetch('/public/alert_events');
                const events = await evRes.json();
                const evTbody = document.getElementById('alert-events-table');
                if (events.length === 0) {
                    evTbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color: var(--text-muted);">No alert events triggered yet</td></tr>';
                } else {
                    evTbody.innerHTML = events.map(ev => `
                        <tr>
                            <td style="font-family:'JetBrains Mono'; color:var(--danger);">${formatLocalTime(ev.fired_at)}</td>
                            <td><code>${ev.employee_id}</code></td>
                            <td><span class="badge badge-critical" style="margin-right:8px;">ALERT</span><strong style="color: var(--text);">${ev.message}</strong></td>
                        </tr>
                    `).join('');
                }
            } catch (e) {
                console.log('Error loading alert events:', e);
            }

            // Load Configured Alert Rules
            const res = await fetch('/public/alert_rules');
            const rules = await res.json();
            const tbody = document.getElementById('alerts-table');
            if (rules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color: var(--text-muted);">No alert rules configured</td></tr>';
                return;
            }
            tbody.innerHTML = rules.map(r => `
                <tr>
                    <td><strong>${r.name}</strong></td>
                    <td><code>${r.rule_type}</code></td>
                    <td><code>${JSON.stringify(r.params)}</code></td>
                    <td>${r.notify_via}</td>
                    <td>${r.notify_target || '—'}</td>
                    <td><span class="badge badge-active">${r.enabled ? 'Enabled' : 'Disabled'}</span></td>
                    <td><button class="btn btn-danger" onclick="deleteAlertRule(${r.id})">Delete</button></td>
                </tr>
            `).join('');
        }

        async function createAlertRule() {
            const name = document.getElementById('rule-name').value;
            const type = document.getElementById('rule-type').value;
            const params = document.getElementById('rule-params').value;
            const target = document.getElementById('rule-target').value;
            if (!name || !target) return alert('Name and target are required!');
            const body = new FormData();
            body.append('name', name);
            body.append('rule_type', type);
            body.append('params', params);
            body.append('notify_via', 'webhook');
            body.append('notify_target', target);
            await fetch('/public/alert_rules', { method: 'POST', body });
            loadAlerts();
        }

        async function deleteAlertRule(id) {
            await fetch('/public/alert_rules/' + id, { method: 'DELETE' });
            loadAlerts();
        }

        // 9. Users & Roles
        async function loadUsers() {
            const res = await fetch('/public/users');
            const users = await res.json();
            const tbody = document.getElementById('users-table');
            tbody.innerHTML = users.map(u => `
                <tr>
                    <td>${u.id}</td>
                    <td><strong>${u.username}</strong></td>
                    <td>${u.display_name}</td>
                    <td><span class="badge badge-info">${u.role}</span></td>
                    <td><span class="badge ${u.active ? 'badge-active' : 'badge-offline'}">${u.active ? 'Active' : 'Disabled'}</span></td>
                </tr>
            `).join('');
        }

        async function createUser() {
            const un = document.getElementById('user-username').value;
            const pw = document.getElementById('user-password').value;
            const dn = document.getElementById('user-display').value;
            const role = document.getElementById('user-role').value;
            if (!un || !pw) return alert('Username and Password required!');
            const body = new FormData();
            body.append('username', un);
            body.append('password', pw);
            body.append('display_name', dn || un);
            body.append('role', role);
            body.append('managed_employee_ids', '[]');
            await fetch('/public/users', { method: 'POST', body });
            loadUsers();
        }

        // 10. Audit Log
        async function loadAudit() {
            const res = await fetch('/public/audit');
            const audits = await res.json();
            const tbody = document.getElementById('audit-table');
            if (audits.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-muted);">No audit entries yet</td></tr>';
                return;
            }
            tbody.innerHTML = audits.map(a => `
                <tr>
                    <td style="font-family:'JetBrains Mono'; color:var(--neon-green);">${formatLocalTime(a.at)}</td>
                    <td><strong>${a.actor}</strong></td>
                    <td><span class="badge badge-info">${a.role}</span></td>
                    <td><code>${a.action}</code></td>
                    <td>${a.target_employee_id || '—'}</td>
                    <td><code>${JSON.stringify(a.detail)}</code></td>
                </tr>
            `).join('');
        }

        // Lightbox helper
        function openLightbox(src, caption) {
            document.getElementById('lightbox-img').src = src;
            document.getElementById('lightbox-caption').innerText = caption;
            document.getElementById('lightbox').style.display = 'flex';
        }
        function closeLightbox(e) {
            document.getElementById('lightbox').style.display = 'none';
        }

        // Initial Load & Polling
        loadOverview();
        setInterval(refreshCurrentTab, 5000);
    </script>
</body>
</html>"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/favicon.ico")
def favicon():
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='#030805'/><rect width='90' height='90' x='5' y='5' rx='16' fill='#000' stroke='#00ff66' stroke-width='4'/><text x='50%' y='68%' font-size='60' text-anchor='middle' fill='#00ff66' font-family='sans-serif' font-weight='bold'>⚡</text></svg>"""
    return Response(content=svg, media_type="image/svg+xml")
