"""
Idle/active detection.

Windows path (production): uses pywin32's GetLastInputInfo via ctypes,
same approach pywin32 wraps -- comparing GetTickCount() to the last
input event's timestamp. This is the real mechanism the spec calls for.

Non-Windows fallback: returns "active" always. This ONLY exists so the
rest of the agent (heartbeat loop, screenshot loop) can be imported and
exercised in this Linux sandbox. It is not a substitute for the real
check -- on a real Windows deployment the ctypes path below is what
actually runs, and it hasn't been tested on real Windows in this
session.
"""

import sys
import time

IDLE_THRESHOLD_SECONDS_DEFAULT = 60


def _windows_idle_seconds():
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    millis_since_input = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return millis_since_input / 1000.0


def get_idle_seconds():
    if sys.platform == "win32":
        return _windows_idle_seconds()
    # Non-Windows fallback for cross-platform testing only.
    return 0.0


def get_status(idle_threshold_seconds=IDLE_THRESHOLD_SECONDS_DEFAULT):
    idle_seconds = get_idle_seconds()
    return "idle" if idle_seconds >= idle_threshold_seconds else "active"


def get_active_window_title():
    """
    Best-effort foreground app name, used for the blacklisted_app alert
    rule. Windows-only; returns None elsewhere.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
        return buff.value
    except Exception:
        return None
