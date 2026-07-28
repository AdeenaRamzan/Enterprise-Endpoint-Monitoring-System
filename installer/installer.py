"""
Installer entry point (what the PyInstaller-built installer.exe runs).

This file is written to spec but its Windows-specific and network-side-
effect steps (winreg autostart, Tailscale join, tray icon) have NOT been
run in this session -- this Linux sandbox can't exercise them for real.
Review and test each step on an actual Windows machine before shipping.

Order of operations:
  1. Run agent.py's consent dialog FIRST -- if declined, stop here and
     do not touch Tailscale, autostart, or anything else.
  2. Join the org Tailscale network using a pre-generated auth key.
  3. Register the agent to auto-start at login.
  4. Start the persistent, visible tray icon.
  5. Launch the agent loop (heartbeat + screenshots + remote-session
     listener) -- same code path as running agent.py directly.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))

import agent as agent_module  # noqa: E402


TAILSCALE_AUTH_KEY = "tskey-auth-REPLACE_ME"  # generated per-org in the Tailscale admin console


def join_tailscale(auth_key: str):
    """
    Bundles/assumes a Tailscale MSI has been silently installed alongside
    this installer (PyInstaller --add-binary, or a prior NSIS/Inno Setup
    step -- not reproduced here). This just runs the join.
    Windows-only command; not run in this session.
    """
    if sys.platform != "win32":
        print("[installer] (non-Windows stub) would run: tailscale up --authkey=... --unattended")
        return
    try:
        subprocess.run(
            ["tailscale", "up", f"--authkey={auth_key}", "--unattended", "--accept-routes"],
            check=True, capture_output=True, text=True,
        )
        print("[installer] joined Tailscale network")
    except subprocess.CalledProcessError as e:
        print(f"[installer] Tailscale join failed: {e.stderr}")
        raise


def register_autostart():
    """
    Registers agent.py (via the packaged agent.exe path) to run at
    login using the Windows Registry Run key. Task Scheduler is an
    equally valid alternative per spec -- Run key chosen here for
    simplicity. Windows-only; not run in this session.
    """
    if sys.platform != "win32":
        print("[installer] (non-Windows stub) would register HKCU Run key autostart")
        return
    import winreg

    exe_path = str(Path(sys.executable).parent / "agent.exe")
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                          0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "MonitoringAgent", 0, winreg.REG_SZ, exe_path)
    winreg.CloseKey(key)
    print("[installer] registered autostart at login")


def main():
    cfg = agent_module.load_config()

    print("[installer] showing consent dialog...")
    if not agent_module.show_consent_dialog(cfg):
        print("[installer] declined -- aborting installation. Nothing else will run.")
        return

    if not agent_module.CONSENT_MARKER.exists():
        agent_module.log_consent_to_backend(cfg)

    join_tailscale(TAILSCALE_AUTH_KEY)
    register_autostart()

    print("[installer] starting tray icon...")
    import tray_icon
    tray_icon.start_tray_icon_thread(cfg)

    print("[installer] handing off to agent loop...")
    agent_module.start_heartbeat_thread(cfg["employee_id"], cfg["backend_url"],
                                        cfg.get("heartbeat_interval_seconds", 7),
                                        cfg.get("idle_threshold_seconds", 60))
    agent_module.start_remote_session_thread(cfg)
    agent_module.capture_and_upload_loop(cfg)


if __name__ == "__main__":
    main()
