"""
Thin REST/WebSocket client the console UI talks to. Kept separate from
the Qt widgets so it can be unit-tested without a display.
"""

import json
from pathlib import Path
import requests

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_backend_url(default="http://127.0.0.1:8000"):
    """Reads console/config.json so IT can point every console at the
    office backend (its Tailscale IP) once, without anyone needing a
    'server address' field in the login screen or to edit Python."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f).get("backend_url", default)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class ApiClient:
    def __init__(self, base_url=None):
        self.base_url = base_url or load_backend_url()
        self.token = None
        self.role = None
        self.display_name = None
        self.username = None

    def login(self, username, password):
        resp = requests.post(f"{self.base_url}/auth/login",
                              data={"username": username, "password": password}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.role = data["role"]
        self.display_name = data["display_name"]
        self.username = username
        return data

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_status(self):
        r = requests.get(f"{self.base_url}/status", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_overview_summary(self):
        r = requests.get(f"{self.base_url}/overview/summary", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_activity(self, employee_id="all", event_type=None, severity=None, limit=200):
        params = {"limit": limit}
        if event_type:
            params["event_type"] = event_type
        if severity:
            params["severity"] = severity
        r = requests.get(f"{self.base_url}/activity/{employee_id}", headers=self._headers(),
                          params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_screenshots(self, employee_id):
        r = requests.get(f"{self.base_url}/screenshots/{employee_id}", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_screenshot_bytes(self, employee_id, screenshot_id):
        r = requests.get(f"{self.base_url}/screenshots/{employee_id}/{screenshot_id}/image",
                          headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.content

    def get_latest_screenshot_bytes(self, employee_id):
        """Returns None (not an error) if the employee has no screenshot yet --
        this is the normal state right after an agent's first install, so the
        thumbnail wall should show a placeholder rather than an error."""
        r = requests.get(f"{self.base_url}/screenshots/{employee_id}/latest",
                          headers=self._headers(), timeout=6)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    def remote_ws_url(self, employee_id):
        """wss://.../ws/remote/console/{employee_id}, with the JWT as a query
        param since browser-less websocket clients here don't send custom
        auth headers on the handshake as reliably as plain HTTP does."""
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/ws/remote/console/{employee_id}?token={self.token}"

    def get_alert_events(self):
        r = requests.get(f"{self.base_url}/alerts/events", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def list_alert_rules(self):
        r = requests.get(f"{self.base_url}/alerts/rules", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def create_alert_rule(self, name, rule_type, params: dict, notify_via, notify_target):
        r = requests.post(f"{self.base_url}/alerts/rules", headers=self._headers(), data={
            "name": name, "rule_type": rule_type, "params": json.dumps(params),
            "notify_via": notify_via, "notify_target": notify_target,
        }, timeout=10)
        r.raise_for_status()
        return r.json()

    def delete_alert_rule(self, rule_id):
        r = requests.delete(f"{self.base_url}/alerts/rules/{rule_id}", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def list_users(self):
        r = requests.get(f"{self.base_url}/admin/users", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def create_user(self, username, password, role, display_name, managed_employee_ids=None):
        r = requests.post(f"{self.base_url}/admin/users", headers=self._headers(), data={
            "username": username, "password": password, "role": role,
            "display_name": display_name,
            "managed_employee_ids": json.dumps(managed_employee_ids or []),
        }, timeout=10)
        r.raise_for_status()
        return r.json()

    def query_audit_log(self, employee_id=None, limit=200):
        params = {"limit": limit}
        if employee_id:
            params["employee_id"] = employee_id
        r = requests.get(f"{self.base_url}/audit", headers=self._headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def start_remote_session(self, employee_id):
        r = requests.post(f"{self.base_url}/remote/start/{employee_id}", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def end_remote_session(self, session_id):
        r = requests.post(f"{self.base_url}/remote/end/{session_id}", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def set_blank_screen(self, employee_id, enabled, color="black"):
        r = requests.post(f"{self.base_url}/remote/blank/{employee_id}",
                           headers=self._headers(), params={"enabled": enabled, "color": color}, timeout=10)
        r.raise_for_status()
        return r.json()

    def list_employees(self):
        r = requests.get(f"{self.base_url}/admin/employees", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def rename_employee(self, employee_id, display_name):
        r = requests.patch(f"{self.base_url}/admin/employees/{employee_id}",
                            headers=self._headers(), data={"display_name": display_name}, timeout=10)
        r.raise_for_status()
        return r.json()
