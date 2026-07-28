import asyncio
import smtplib
import time
from datetime import datetime, time as dtime
from email.mime.text import MIMEText

import httpx

from config import settings
from models import SessionLocal, AlertRule, AlertEvent, Employee
from presence import get_presence

CHECK_INTERVAL_SECONDS = 30
# Don't re-fire the same rule for the same employee more than once per window
RE_ALERT_COOLDOWN_SECONDS = 15 * 60


def _in_after_hours_window(now: dtime, start: str, end: str) -> bool:
    start_t = dtime.fromisoformat(start)
    end_t = dtime.fromisoformat(end)
    if start_t <= end_t:
        return start_t <= now <= end_t
    return now >= start_t or now <= end_t  # window wraps midnight


def _recently_fired(db, rule_id: int, employee_id: str) -> bool:
    last = (
        db.query(AlertEvent)
        .filter(AlertEvent.rule_id == rule_id, AlertEvent.employee_id == employee_id)
        .order_by(AlertEvent.fired_at.desc())
        .first()
    )
    if not last:
        return False
    return (datetime.utcnow() - last.fired_at).total_seconds() < RE_ALERT_COOLDOWN_SECONDS


def _fire_alert(db, rule: AlertRule, employee_id: str, message: str):
    event = AlertEvent(rule_id=rule.id, employee_id=employee_id, message=message)
    db.add(event)
    db.commit()
    _notify(rule, message)


def _notify(rule: AlertRule, message: str):
    try:
        if rule.notify_via == "webhook":
            httpx.post(rule.notify_target, json={"text": message}, timeout=5)
        elif rule.notify_via == "smtp" and settings.smtp_host:
            msg = MIMEText(message)
            msg["Subject"] = "Monitoring Alert"
            msg["From"] = settings.smtp_from
            msg["To"] = rule.notify_target
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as server:
                server.starttls()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(settings.smtp_from, [rule.notify_target], msg.as_string())
    except Exception as e:
        print(f"[alerts] notify failed for rule {rule.id}: {e}")


def evaluate_rules_once():
    db = SessionLocal()
    try:
        rules = db.query(AlertRule).filter(AlertRule.enabled == True).all()  # noqa: E712
        employees = db.query(Employee).filter(Employee.active == True).all()  # noqa: E712

        for rule in rules:
            for emp in employees:
                presence = get_presence(emp.employee_id)
                if presence["status"] == "offline":
                    continue

                triggered, message = False, None

                if rule.rule_type == "idle_minutes":
                    idle_since = presence.get("idle_since")
                    if presence["status"] == "idle" and idle_since:
                        idle_minutes = (time.time() - idle_since) / 60
                        threshold = rule.params.get("minutes", 30)
                        if idle_minutes >= threshold:
                            triggered = True
                            message = f"{emp.display_name} has been inactive for {int(idle_minutes)} minutes"

                elif rule.rule_type == "after_hours":
                    now_t = datetime.now().time()
                    if presence["status"] == "active" and _in_after_hours_window(
                        now_t, rule.params.get("start", "18:00"), rule.params.get("end", "08:00")
                    ):
                        triggered = True
                        message = f"{emp.display_name} is active outside working hours"

                elif rule.rule_type == "blacklisted_app":
                    app = (presence.get("active_app") or "").lower()
                    target_app = rule.params.get("app_name", "").lower()
                    if target_app and target_app in app:
                        triggered = True
                        message = f"{emp.display_name} opened a restricted application ({rule.params.get('app_name')})"

                if triggered and not _recently_fired(db, rule.id, emp.employee_id):
                    _fire_alert(db, rule, emp.employee_id, message)
    finally:
        db.close()


async def alert_engine_loop():
    while True:
        try:
            evaluate_rules_once()
        except Exception as e:
            print(f"[alerts] evaluation error: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
