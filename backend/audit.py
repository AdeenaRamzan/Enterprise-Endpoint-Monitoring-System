from sqlalchemy.orm import Session
from models import AuditLog, User


def log_audit(db: Session, actor: User, action: str, target_employee_id: str = None, detail: dict = None):
    entry = AuditLog(
        actor_username=actor.username,
        actor_role=actor.role.value,
        action=action,
        target_employee_id=target_employee_id,
        detail=detail or {},
    )
    db.add(entry)
    db.commit()
