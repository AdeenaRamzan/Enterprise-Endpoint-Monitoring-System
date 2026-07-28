from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import bcrypt
from sqlalchemy.orm import Session

from config import settings
from models import get_db, User, Role

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))


def create_access_token(user: User) -> str:
    payload = {
        "sub": user.username,
        "role": user.role.value,
        "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_token(token)
    username = payload.get("sub")
    user = db.query(User).filter(User.username == username, User.active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_roles(*allowed: Role):
    """
    Dependency factory for RBAC. This is enforced server-side on every
    protected route -- a Manager-role JWT gets a 403 from FastAPI itself
    if it hits an ITStaff/SuperAdmin-only route, regardless of what the
    console UI does or doesn't show.
    """
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail=f"Role {user.role.value} not permitted")
        return user
    return dependency


require_admin = require_roles(Role.SUPERADMIN)
require_it = require_roles(Role.SUPERADMIN, Role.ITSTAFF)
require_read = require_roles(Role.SUPERADMIN, Role.ITSTAFF, Role.MANAGER, Role.VIEWER)
require_gallery = require_roles(Role.SUPERADMIN, Role.ITSTAFF, Role.MANAGER)


def visible_employee_ids(user: User, db: Session) -> Optional[List[str]]:
    """
    None means "all employees" (IT/SuperAdmin/Viewer-with-no-restriction).
    A list means the caller is restricted to exactly those employee_ids
    -- this is how a Manager's JWT only ever sees their own team, even
    if they guess another employee_id and call the API directly.
    """
    if user.role in (Role.SUPERADMIN, Role.ITSTAFF):
        return None
    return user.managed_employee_ids or []
