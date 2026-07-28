"""
Manager mode should have zero re-login friction: double-click the
desktop shortcut, see the grid. We persist the JWT locally and try it
before showing the login screen. If the token has expired, the API
client's first request will 401 and we fall back to login.
"""

import json
from pathlib import Path

SESSION_FILE = Path.home() / ".monitoring_console_session.json"


def save_session(api):
    SESSION_FILE.write_text(json.dumps({
        "token": api.token, "role": api.role,
        "display_name": api.display_name, "username": api.username,
        "base_url": api.base_url,
    }))


def load_session():
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return None


def clear_session():
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
