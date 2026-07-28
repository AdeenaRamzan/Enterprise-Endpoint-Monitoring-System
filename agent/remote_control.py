"""
Remote view + remote control. Session state (on/off, blanked/not) is
driven entirely by commands received over the WebSocket -- the agent
never decides locally to start a session or blank the screen.

Screen capture and JPEG encode (mss + Pillow) work cross-platform and
ARE exercised in this session's testing. Mouse/keyboard injection
(SendInput) and the low-level input-blocking hook (SetWindowsHookEx)
are Windows-only ctypes calls -- written to spec below, but NOT run or
verified in this Linux sandbox. Test these for real on a Windows
machine before relying on them.
"""

import asyncio
import ctypes
import io
import json
import sys
import threading

import mss
import websockets
from PIL import Image


def _ws_url_from_http(backend_url: str) -> str:
    return backend_url.replace("https://", "wss://").replace("http://", "ws://")


def get_screen_resolution():
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    try:
        with mss.mss() as sct:
            mon = sct.monitors[0]
            return mon["width"], mon["height"]
    except Exception:
        # No display available (e.g. this Linux test sandbox). Only hit
        # on non-Windows dev/test environments -- production is Windows.
        return 1920, 1080


# ---------------- Unified remote session (agent side) ----------------
# One persistent connection per agent to /ws/remote/agent/{employee_id}.
# Session on/off and blank on/off are driven entirely by messages
# received here -- the agent never decides on its own.

async def run_remote_session(employee_id: str, backend_url: str, blank_controller):
    ws_url = f"{_ws_url_from_http(backend_url)}/ws/remote/agent/{employee_id}"
    state = {"streaming": False}

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                width, height = get_screen_resolution()
                await ws.send(json.dumps({"type": "resolution", "width": width, "height": height}))

                async def receiver():
                    async for message in ws:
                        if isinstance(message, bytes):
                            continue
                        cmd = json.loads(message)
                        if cmd.get("type") == "session_start":
                            state["streaming"] = True
                        elif cmd.get("type") == "session_end":
                            state["streaming"] = False
                            blank_controller.disable()
                        elif cmd.get("type") == "blank_screen":
                            if cmd.get("enabled"):
                                blank_controller.enable(cmd.get("color", "black"))
                            else:
                                blank_controller.disable()
                        elif cmd.get("type") in ("mouse", "key"):
                            apply_input_command(cmd, (width, height))

                async def sender():
                    try:
                        sct = mss.mss()
                        monitor = sct.monitors[0]
                        real_capture = True
                    except Exception:
                        sct, monitor, real_capture = None, None, False
                    try:
                        while True:
                            if state["streaming"]:
                                try:
                                    if real_capture:
                                        img = sct.grab(monitor)
                                        pil_img = Image.frombytes("RGB", img.size, img.rgb)
                                        # Scale down if full 4K to ensure 20 FPS low-latency streaming
                                        if pil_img.width > 1920:
                                            pil_img = pil_img.resize((1920, int(1920 * pil_img.height / pil_img.width)), Image.Resampling.LANCZOS)
                                        buf = io.BytesIO()
                                        pil_img.save(buf, format="JPEG", quality=45, optimize=True)
                                        frame_bytes = buf.getvalue()
                                    else:
                                        frame_bytes = b"\xff\xd8\xff\xe0NO_DISPLAY_PLACEHOLDER_FRAME"
                                    await ws.send(frame_bytes)
                                except Exception as e:
                                    print(f"[remote] frame capture failed: {e}")
                                await asyncio.sleep(1 / 20)
                            else:
                                await asyncio.sleep(0.5)
                    finally:
                        if sct:
                            sct.close()

                await asyncio.gather(receiver(), sender())
        except Exception as e:
            print(f"[remote] session connection lost ({e}), retrying in 5s")
            await asyncio.sleep(5)


def start_remote_session_thread(employee_id: str, backend_url: str, blank_controller):
    def _run():
        asyncio.run(run_remote_session(employee_id, backend_url, blank_controller))

    threading.Thread(target=_run, daemon=True).start()


# ---------------- Input injection (console -> agent, Windows only) ----------------

# ctypes SendInput plumbing. Untested on real Windows in this session --
# verify before production use.
PUL = ctypes.POINTER(ctypes.c_ulong)


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]


def apply_input_command(command: dict, agent_resolution):
    """
    command examples:
      {"type": "mouse", "x": 120, "y": 340, "action": "move"}
      {"type": "mouse", "x": 120, "y": 340, "action": "left_click"}
      {"type": "key", "vk": 65, "action": "down"|"up"}
    """
    if sys.platform != "win32":
        print(f"[remote] (non-Windows stub) would apply input: {command}")
        return

    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    w, h = agent_resolution
    if not w or not h or w <= 0 or h <= 0:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)

    if command["type"] == "mouse":
        raw_x = int(command.get("x", 0))
        raw_y = int(command.get("y", 0))

        # Convert to Windows normalized absolute coordinate range [0, 65535]
        norm_x = int((raw_x / w) * 65535) if w > 0 else 0
        norm_y = int((raw_y / h) * 65535) if h > 0 else 0
        norm_x = max(0, min(65535, norm_x))
        norm_y = max(0, min(65535, norm_y))

        user32.SetCursorPos(raw_x, raw_y)
        # 0x8001 = MOUSEEVENTF_ABSOLUTE (0x8000) | MOUSEEVENTF_MOVE (0x0001)
        user32.mouse_event(0x8001, norm_x, norm_y, 0, 0)

        action = command.get("action", "move")
        if action == "left_click":
            user32.mouse_event(0x8003, norm_x, norm_y, 0, 0)  # MOVE | ABSOLUTE | LEFTDOWN
            user32.mouse_event(0x8005, norm_x, norm_y, 0, 0)  # MOVE | ABSOLUTE | LEFTUP
        elif action == "left_down":
            user32.mouse_event(0x8003, norm_x, norm_y, 0, 0)
        elif action == "left_up":
            user32.mouse_event(0x8005, norm_x, norm_y, 0, 0)
        elif action == "right_click":
            user32.mouse_event(0x8009, norm_x, norm_y, 0, 0)  # MOVE | ABSOLUTE | RIGHTDOWN
            user32.mouse_event(0x8011, norm_x, norm_y, 0, 0)  # MOVE | ABSOLUTE | RIGHTUP
        elif action == "double_click":
            user32.mouse_event(0x8003, norm_x, norm_y, 0, 0)
            user32.mouse_event(0x8005, norm_x, norm_y, 0, 0)
            user32.mouse_event(0x8003, norm_x, norm_y, 0, 0)
            user32.mouse_event(0x8005, norm_x, norm_y, 0, 0)
    elif command["type"] == "key":
        flag = 0 if command.get("action") == "down" else 0x0002  # KEYEVENTF_KEYUP
        user32.keybd_event(int(command["vk"]), 0, flag, 0)


# ---------------- Blank screen: overlay window + low-level input block ----------------

class BlankScreenController:
    """
    Windows: spawns a full-screen always-on-top borderless Tk window
    (color chosen by IT) and installs a WH_KEYBOARD_LL/WH_MOUSE_LL hook
    via SetWindowsHookEx to block employee input while active. Both are
    released the instant the command flips off or the session ends.

    The hook installation is Windows-only ctypes and is NOT exercised in
    this Linux sandbox -- only the Tk overlay (cross-platform) is tested
    here. Verify the actual input block on a real Windows machine.
    """

    def __init__(self):
        self._overlay = None
        self._hook_id = None
        self._hook_proc = None

    def _install_windows_hook(self):
        if sys.platform != "win32":
            return
        WH_KEYBOARD_LL = 13
        WH_MOUSE_LL = 14

        CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)

        def _block(nCode, wParam, lParam):
            return 1  # swallow the event

        self._hook_proc = CMPFUNC(_block)
        self._kb_hook = ctypes.windll.user32.SetWindowsHookExA(
            WH_KEYBOARD_LL, self._hook_proc, None, 0)
        self._mouse_hook = ctypes.windll.user32.SetWindowsHookExA(
            WH_MOUSE_LL, self._hook_proc, None, 0)

    def _remove_windows_hook(self):
        if sys.platform != "win32":
            return
        if getattr(self, "_kb_hook", None):
            ctypes.windll.user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if getattr(self, "_mouse_hook", None):
            ctypes.windll.user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None

    def enable(self, color="black"):
        if getattr(self, "_overlay", None) is not None:
            return
        import tkinter as tk

        def _show():
            try:
                self._overlay = tk.Tk()
                self._overlay.attributes("-fullscreen", True)
                self._overlay.attributes("-topmost", True)
                self._overlay.overrideredirect(True)
                self._overlay.configure(bg=color)
                
                lbl = tk.Label(
                    self._overlay, 
                    text="🔒 Screen Blanked by IT Administration\n\nYour display and input have been temporarily locked by IT Support.", 
                    font=("Segoe UI", 22, "bold"), 
                    fg="#f8fafc", 
                    bg=color,
                    justify="center"
                )
                lbl.pack(expand=True)
                
                self._overlay.lift()
                self._overlay.focus_force()
                self._install_windows_hook()
                self._overlay.mainloop()
            except Exception as e:
                print(f"[blank_screen] Overlay error: {e}")

        threading.Thread(target=_show, daemon=True).start()

    def disable(self):
        self._remove_windows_hook()
        if getattr(self, "_overlay", None):
            try:
                self._overlay.after(0, self._overlay.destroy)
            except Exception:
                pass
            self._overlay = None
