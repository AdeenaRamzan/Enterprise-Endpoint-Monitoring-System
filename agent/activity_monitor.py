"""
Feeds the backend's /activity endpoint. Four collectors, each a
background thread with its own poll loop:

- browser_history_loop  -- reads the Chrome/Edge SQLite history file
  directly (a local, plaintext-readable file -- not a browser
  extension, not JS, nothing web-frontend about it)
- usb_loop               -- polls logical drives via ctypes, diffs
  against the previous snapshot for connect/disconnect
- file_activity_loop     -- polls a small set of watched folders
  (Desktop/Documents/Downloads) for created/modified/deleted files;
  flags files that land on a currently-connected USB drive as a
  possible transfer
- flagged_app_loop       -- polls the foreground process name against
  a configurable list (WhatsApp, Telegram, etc.); on a flagged app
  gaining focus, logs an APP event AND raises the shared
  `screenshot_boost` flag so agent.py's screenshot loop captures more
  often while that app is in focus -- this is the "catch what was
  sent" mechanism. See the module docstring in agent.py / the project
  README for why this project does NOT do keystroke logging: this
  achieves the same practical goal (IT can see what appeared on
  screen) without the much larger blast radius of a system-wide
  keylogger that would also capture passwords typed anywhere.

Windows-only in practice (SQLite paths, ctypes calls below assume
Windows conventions) -- guarded so the rest of the agent still runs
on this non-Windows sandbox for testing, but none of these four
collectors have been exercised against a real Windows machine. Same
honesty caveat as idle_tracking.py / remote_control.py.
"""

import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests


def webkit_time_to_datetime(webkit_ts: int) -> datetime:
    """Chrome/Edge store visit times as microseconds since 1601-01-01."""
    if not webkit_ts:
        return datetime.utcnow()
    return datetime(1601, 1, 1) + timedelta(microseconds=webkit_ts)


def post_event(backend_url, employee_id, event_type, summary, severity="info", detail=None, occurred_at=None):
    try:
        requests.post(
            f"{backend_url}/activity",
            data={
                "employee_id": employee_id, "event_type": event_type, "severity": severity,
                "summary": summary, "occurred_at": (occurred_at or datetime.utcnow()).isoformat(),
                "detail": json.dumps(detail or {}),
            },
            timeout=10,
        )
    except requests.RequestException:
        pass  # best-effort; the next poll cycle will just try again with fresh data


class ScreenshotBoost:
    """Shared flag the flagged-app collector sets; agent.py's screenshot
    loop reads it to shorten its interval while a messaging app is focused."""
    def __init__(self):
        self._active_until = 0.0
        self._lock = threading.Lock()

    def raise_for(self, seconds: float):
        with self._lock:
            self._active_until = max(self._active_until, time.time() + seconds)

    def is_active(self) -> bool:
        with self._lock:
            return time.time() < self._active_until


# ---------- Browser history ----------

def _candidate_history_files():
    if sys.platform != "win32":
        return []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    files = []
    for browser_dir in [local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data"]:
        if browser_dir.exists():
            try:
                for hist in browser_dir.glob("*/History"):
                    files.append(hist)
            except Exception:
                pass
    return files


def _get_max_webkit_ts(history_path: Path):
    if not history_path.exists():
        return 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(tmp_fd)
    try:
        shutil.copy2(history_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cur = conn.execute("SELECT MAX(last_visit_time) FROM urls")
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] else 0
    except sqlite3.Error:
        return 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _read_new_history_rows(history_path: Path, since_webkit_ts: int):
    """The History file is locked while the browser runs, so read from a
    temp copy -- this is the standard workaround, not a hack."""
    if not history_path.exists():
        return [], since_webkit_ts
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(tmp_fd)
    try:
        shutil.copy2(history_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        rows = conn.execute(
            "SELECT url, title, last_visit_time FROM urls "
            "WHERE last_visit_time > ? ORDER BY last_visit_time ASC LIMIT 200",
            (since_webkit_ts,),
        ).fetchall()
        conn.close()
        new_max = max((r[2] for r in rows), default=since_webkit_ts)
        return rows, new_max
    except sqlite3.Error:
        return [], since_webkit_ts
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def browser_history_loop(employee_id, backend_url, screenshot_boost: ScreenshotBoost = None, poll_seconds=10):
    candidate_files = _candidate_history_files()
    last_seen = {str(p): _get_max_webkit_ts(p) for p in candidate_files}
    while True:
        candidate_files = _candidate_history_files()
        for history_path in candidate_files:
            key = str(history_path)
            if key not in last_seen:
                last_seen[key] = _get_max_webkit_ts(history_path)
                continue

            rows, new_max = _read_new_history_rows(history_path, last_seen[key])
            last_seen[key] = new_max
            for url, title, visit_time in rows:
                if url.startswith("http://") or url.startswith("https://"):
                    is_flagged = any(k in url.lower() for k in ["whatsapp", "telegram", "discord", "signal", "facebook", "instagram"])
                    if is_flagged and screenshot_boost:
                        screenshot_boost.raise_for(20)
                    post_event(
                        backend_url, employee_id, "BROWSER",
                        severity="warn" if is_flagged else "info",
                        summary=f"{url} ({title or 'no title'})",
                        detail={"url": url, "title": title, "browser": history_path.parent.name},
                        occurred_at=webkit_time_to_datetime(visit_time),
                    )
        time.sleep(poll_seconds)


# ---------- USB connect/disconnect ----------

DRIVE_REMOVABLE = 2


def _removable_drives():
    if sys.platform != "win32":
        return {}
    drives = {}
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = f"{chr(65 + i)}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(letter) == DRIVE_REMOVABLE:
            vol_name = ctypes.create_unicode_buffer(261)
            ctypes.windll.kernel32.GetVolumeInformationW(letter, vol_name, 260, None, None, None, None, 0)
            drives[letter] = vol_name.value or "Removable Disk"
    return drives


def usb_loop(employee_id, backend_url, shared_drive_state, poll_seconds=3):
    """shared_drive_state is a dict this loop keeps updated with the
    currently-connected removable drives, so file_activity_loop can tag
    file events as 'possible USB transfer' without polling twice."""
    previous = _removable_drives()
    shared_drive_state.update(previous)
    while True:
        current = _removable_drives()
        for letter, name in current.items():
            if letter not in previous:
                post_event(backend_url, employee_id, "USB", severity="warn",
                            summary=f"Drive {letter} Connected ({name})",
                            detail={"drive": letter, "label": name, "action": "connected"})
        for letter, name in previous.items():
            if letter not in current:
                post_event(backend_url, employee_id, "USB", severity="warn",
                            summary=f"Drive {letter} Disconnected ({name})",
                            detail={"drive": letter, "label": name, "action": "disconnected"})
        previous = current
        shared_drive_state.clear()
        shared_drive_state.update(current)
        time.sleep(poll_seconds)


# ---------- File activity (incl. USB-transfer flagging) ----------

def _snapshot_folder(folder: Path, max_files=500):
    if not folder.exists():
        return {}
    snap = {}
    try:
        for i, entry in enumerate(folder.iterdir()):
            if i >= max_files or not entry.is_file():
                continue
            try:
                st = entry.stat()
                snap[str(entry)] = (st.st_size, st.st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return snap


def file_activity_loop(employee_id, backend_url, watched_folders, shared_drive_state, poll_seconds=15):
    previous = {f: _snapshot_folder(Path(f)) for f in watched_folders}
    while True:
        for folder in watched_folders:
            current = _snapshot_folder(Path(folder))
            prev = previous[folder]
            for path, (size, mtime) in current.items():
                on_usb = any(path.upper().startswith(letter.upper()) for letter in shared_drive_state)
                if path not in prev:
                    post_event(
                        backend_url, employee_id, "FILE",
                        severity="warn" if on_usb else "info",
                        summary=(f"Possible transfer to USB: {path}" if on_usb else f"File created: {path}"),
                        detail={"path": path, "size": size, "action": "created", "on_removable_drive": on_usb},
                    )
                elif prev[path] != (size, mtime):
                    post_event(backend_url, employee_id, "FILE",
                                summary=f"File modified: {path}",
                                detail={"path": path, "size": size, "action": "modified"})
            for path in prev:
                if path not in current:
                    post_event(backend_url, employee_id, "FILE",
                                summary=f"File removed: {path}",
                                detail={"path": path, "action": "deleted"})
            previous[folder] = current
        time.sleep(poll_seconds)


# ---------- Flagged applications (messaging apps etc.) ----------

DEFAULT_FLAGGED_KEYWORDS = {
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "discord": "Discord",
    "signal": "Signal",
}


def _foreground_process_name():
    if sys.platform != "win32":
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    return None


def flagged_app_loop(employee_id, backend_url, screenshot_boost: ScreenshotBoost,
                      boost_seconds=20, poll_seconds=2):
    last_flagged = None
    while True:
        proc = _foreground_process_name() or ""
        matched_name = None
        for key, friendly in DEFAULT_FLAGGED_KEYWORDS.items():
            if key in proc:
                matched_name = friendly
                break

        if matched_name:
            screenshot_boost.raise_for(boost_seconds)
            if proc != last_flagged:
                post_event(backend_url, employee_id, "APP", severity="warn",
                            summary=f"Flagged app in use: {matched_name} ({proc})",
                            detail={"process": proc, "friendly_name": matched_name})
            last_flagged = proc
        else:
            last_flagged = None
        time.sleep(poll_seconds)


def start_activity_monitor_threads(employee_id, backend_url, screenshot_boost: ScreenshotBoost,
                                    watched_folders=None):
    """Called once from agent.py's main(). shared_drive_state lets the
    file-activity loop know which drives are currently removable without
    a second USB poll."""
    shared_drive_state = {}
    watched_folders = watched_folders or [
        str(Path.home() / "Desktop"), str(Path.home() / "Documents"), str(Path.home() / "Downloads"),
    ]
    threads = [
        threading.Thread(target=browser_history_loop, args=(employee_id, backend_url, screenshot_boost), daemon=True),
        threading.Thread(target=usb_loop, args=(employee_id, backend_url, shared_drive_state), daemon=True),
        threading.Thread(target=file_activity_loop,
                          args=(employee_id, backend_url, watched_folders, shared_drive_state), daemon=True),
        threading.Thread(target=flagged_app_loop, args=(employee_id, backend_url, screenshot_boost), daemon=True),
    ]
    for t in threads:
        t.start()
    return threads
