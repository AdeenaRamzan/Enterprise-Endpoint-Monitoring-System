"""
Full agent: consent gate -> heartbeat thread + remote-session thread +
screenshot loop, all driven by config.json.

Run:
    pip install requests mss Pillow websockets --break-system-packages
    python agent.py

Windows-specific behavior (idle detection via GetLastInputInfo, SendInput,
SetWindowsHookEx for blank-screen input blocking, GetSystemMetrics for
resolution) is implemented in idle_tracking.py and remote_control.py with
ctypes, guarded by `sys.platform == "win32"` checks so this file itself
runs the same way on any OS. Those Windows code paths have NOT been
executed on real Windows in this session -- verify there before trusting
them in production. Everything else here (consent, screenshot capture,
heartbeat over WebSocket, remote command relay, blank overlay window) has
been tested against the real backend.
"""

import asyncio
import json
import socket
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog

import requests
import mss
import mss.tools

from heartbeat_client import start_heartbeat_thread
from remote_control import run_remote_session, BlankScreenController
from activity_monitor import start_activity_monitor_threads, ScreenshotBoost

import sys

if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', sys.executable))
    EXE_DIR = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).parent
    EXE_DIR = Path(__file__).parent

CONFIG_PATH = EXE_DIR / "config.json"
CONSENT_MARKER = EXE_DIR / ".consent_given"
DEFAULT_BACKEND_URL = "http://192.168.100.14:8000"


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    cfg["backend_url"] = DEFAULT_BACKEND_URL
    if not cfg.get("employee_id"):
        cfg["employee_id"] = socket.gethostname()
    if not cfg.get("policy_version"):
        cfg["policy_version"] = "v1.0"
    if not cfg.get("policy_text_path"):
        cfg["policy_text_path"] = str(BUNDLE_DIR / "monitoring_policy.txt")
    if not cfg.get("local_screenshot_dir"):
        cfg["local_screenshot_dir"] = str(EXE_DIR / "screenshots")
    if "delete_after_upload" not in cfg:
        cfg["delete_after_upload"] = True
    if "screenshot_interval_seconds" not in cfg:
        cfg["screenshot_interval_seconds"] = 10
    if "heartbeat_interval_seconds" not in cfg:
        cfg["heartbeat_interval_seconds"] = 7
    if "idle_threshold_seconds" not in cfg:
        cfg["idle_threshold_seconds"] = 60
    return cfg


def show_consent_dialog(cfg):
    if CONSENT_MARKER.exists():
        return True

    accepted = {"value": False}
    root = tk.Tk()
    root.title("Monitoring Consent Required")
    root.geometry("520x440")
    root.resizable(False, False)

    message = (
        "This computer will be monitored by the IT department:\n\n"
        "  •  A screenshot is captured every 10 seconds (more often\n"
        "     briefly while a messaging app like WhatsApp is in focus)\n"
        "  •  Active / idle time is tracked\n"
        "  •  Browser history, USB drive connections, file activity in\n"
        "     Desktop/Documents/Downloads, and use of flagged apps are\n"
        "     logged\n"
        "  •  IT may start a remote screen view or remote control\n"
        "     session for support, with a visible on-screen indicator\n"
        "  •  Every access to your data by IT/managers is logged\n\n"
        "You must click \"I Accept\" to continue installation. If you\n"
        "click Decline, installation will stop and nothing will be\n"
        "monitored on this machine."
    )
    tk.Label(root, text=message, justify="left", wraplength=480, padx=20, pady=20).pack()

    def open_policy():
        policy_path = (BUNDLE_DIR / cfg.get("policy_text_path", "monitoring_policy.txt")).resolve()
        webbrowser.open(f"file://{policy_path}")

    tk.Button(root, text="View full company monitoring policy", command=open_policy).pack(pady=(0, 10))

    def on_accept():
        accepted["value"] = True
        root.destroy()

    def on_decline():
        accepted["value"] = False
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="Decline", width=15, command=on_decline).pack(side="left", padx=10)
    tk.Button(btn_frame, text="I Accept", width=15, command=on_accept, bg="#2e7d32", fg="white").pack(side="left", padx=10)

    root.protocol("WM_DELETE_WINDOW", on_decline)
    root.mainloop()
    return accepted["value"]


def log_consent_to_backend(cfg):
    while True:
        try:
            resp = requests.post(
                f"{cfg['backend_url']}/consent",
                data={"employee_id": cfg["employee_id"], "hostname": socket.gethostname(),
                      "policy_version": cfg.get("policy_version", "v1.0")},
                timeout=10,
            )
            resp.raise_for_status()
            CONSENT_MARKER.write_text(resp.json()["accepted_at"])
            break
        except requests.exceptions.RequestException as e:
            server_url = cfg.get("backend_url", "http://127.0.0.1:8000")
            print(f"[agent error] Could not connect to {server_url}: {e}")
            root = tk.Tk()
            root.withdraw()
            
            new_ip = simpledialog.askstring(
                "IT Server Connection Failed",
                f"Could not connect to IT Server at:\n{server_url}\n\n"
                "Please enter your IT Server IP Address (e.g. 192.168.100.50):",
                initialvalue=server_url.replace("http://", "").replace(":8000", "")
            )
            if new_ip and new_ip.strip():
                clean_ip = new_ip.strip()
                if not clean_ip.startswith("http://"):
                    clean_ip = f"http://{clean_ip}"
                if ":8000" not in clean_ip:
                    clean_ip = f"{clean_ip}:8000"
                cfg["backend_url"] = clean_ip
                try:
                    with open(CONFIG_PATH, "w") as f:
                        json.dump(cfg, f, indent=2)
                except Exception:
                    pass
            else:
                sys.exit(1)


def start_remote_session_thread(cfg):
    blank_controller = BlankScreenController()

    def _run():
        asyncio.run(run_remote_session(cfg["employee_id"], cfg["backend_url"], blank_controller))

    threading.Thread(target=_run, daemon=True).start()


def capture_and_upload_loop(cfg, screenshot_boost: ScreenshotBoost = None):
    local_dir = Path(cfg["local_screenshot_dir"])
    local_dir.mkdir(parents=True, exist_ok=True)
    print(f"[agent] monitoring active for {cfg['employee_id']}. Ctrl+C to stop.")

    normal_interval = cfg["screenshot_interval_seconds"]
    boosted_interval = cfg.get("boosted_screenshot_interval_seconds", 3)

    with mss.mss() as sct:
        monitor = sct.monitors[0]
        while True:
            captured_at = datetime.utcnow().isoformat()
            safe_ts = captured_at.replace(":", "-")
            local_path = local_dir / f"{safe_ts}.png"

            img = sct.grab(monitor)
            mss.tools.to_png(img.rgb, img.size, output=str(local_path))

            try:
                with open(local_path, "rb") as f:
                    resp = requests.post(
                        f"{cfg['backend_url']}/upload_screenshot",
                        data={"employee_id": cfg["employee_id"], "captured_at": captured_at},
                        files={"file": (local_path.name, f, "image/png")},
                        timeout=15,
                    )
                if resp.status_code == 200 and cfg.get("delete_after_upload", True):
                    local_path.unlink()
                else:
                    print(f"[agent] upload failed ({resp.status_code}), keeping local copy")
            except requests.RequestException as e:
                print(f"[agent] backend unreachable, keeping local copy: {e}")

            # Boosted while a flagged messaging app (WhatsApp etc.) has
            # recently had focus -- see activity_monitor.flagged_app_loop.
            # This is the mechanism for "see what was sent" without a
            # system-wide keylogger; screenshots are already fully
            # consented to, this just takes more of them, briefly.
            interval = boosted_interval if (screenshot_boost and screenshot_boost.is_active()) else normal_interval
            time.sleep(interval)


def main():
    cfg = load_config()

    if not show_consent_dialog(cfg):
        messagebox.showinfo("Installation Stopped", "You declined. No monitoring will occur on this machine.")
        return

    log_consent_to_backend(cfg)

    start_heartbeat_thread(cfg["employee_id"], cfg["backend_url"],
                            interval_seconds=cfg.get("heartbeat_interval_seconds", 7),
                            idle_threshold_seconds=cfg.get("idle_threshold_seconds", 60))
    start_remote_session_thread(cfg)

    screenshot_boost = ScreenshotBoost()
    start_activity_monitor_threads(cfg["employee_id"], cfg["backend_url"], screenshot_boost)

    try:
        capture_and_upload_loop(cfg, screenshot_boost)
    except KeyboardInterrupt:
        print("\n[agent] stopped.")


if __name__ == "__main__":
    main()
