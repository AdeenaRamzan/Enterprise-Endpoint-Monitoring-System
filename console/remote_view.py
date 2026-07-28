"""
Live remote view: the part that was completely missing before. The
backend already relayed frames agent -> console and commands
console -> agent (see backend/ws.py + the /ws/remote/console/{id}
route) -- nothing on the console side ever connected to it. This file
is that missing half.

Uses the `websockets` library (already a dependency of agent/, so this
adds nothing new to the project's dependency footprint) inside a
background thread running its own asyncio loop, and talks to the Qt
main thread only through signals -- Qt widgets are not thread-safe to
touch directly from another thread.
"""

import asyncio
import json
import threading

import websockets
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy


class RemoteFrameClient(QObject):
    """
    Owns the background asyncio thread. Call start() once; call
    send_command(dict) any number of times from the Qt thread; call
    stop() to tear down. Emits Qt signals so the widget can update
    safely on the main thread.
    """
    frame_received = pyqtSignal(bytes)
    resolution_received = pyqtSignal(int, int)
    connection_state = pyqtSignal(str)  # "connecting" | "connected" | "disconnected" | "error:<msg>"

    def __init__(self, ws_url: str):
        super().__init__()
        self._ws_url = ws_url
        self._loop = None
        self._ws = None
        self._thread = None
        self._stop_flag = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close(), self._loop)

    def send_command(self, command: dict):
        if self._loop and self._ws and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send(command), self._loop)

    async def _send(self, command):
        try:
            await self._ws.send(json.dumps(command))
        except Exception:
            pass  # connection likely closing; nothing useful to do here

    async def _close(self):
        if self._ws:
            await self._ws.close()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        self.connection_state.emit("connecting")
        try:
            async with websockets.connect(self._ws_url, max_size=None) as ws:
                self._ws = ws
                self.connection_state.emit("connected")
                async for message in ws:
                    if self._stop_flag.is_set():
                        break
                    if isinstance(message, (bytes, bytearray)):
                        self.frame_received.emit(bytes(message))
                    else:
                        try:
                            data = json.loads(message)
                            if data.get("type") == "resolution":
                                self.resolution_received.emit(data["width"], data["height"])
                        except (json.JSONDecodeError, KeyError):
                            pass
        except Exception as e:
            self.connection_state.emit(f"error:{e}")
            return
        self.connection_state.emit("disconnected")


class RemoteScreenLabel(QLabel):
    """
    The video surface. Captures mouse/keyboard while it has focus and
    reports events already scaled from *this widget's* current pixel
    size to the agent's real screen resolution -- so a click lands in
    the right spot on the employee's monitor regardless of how large
    or small this window is on IT's side.
    """
    mouse_event = pyqtSignal(dict)
    key_event = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #0b0f14; color: #64748b;")
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setText("Waiting for video…")
        self.agent_width = None
        self.agent_height = None
        self.input_enabled = False

    def _scaled_xy(self, pos):
        if not self.agent_width or not self.width() or not self.height():
            return None
        sx = self.agent_width / self.width()
        sy = self.agent_height / self.height()
        return int(pos.x() * sx), int(pos.y() * sy)

    def mousePressEvent(self, event):
        if not self.input_enabled:
            return
        xy = self._scaled_xy(event.position().toPoint())
        if xy:
            button = "left_click" if event.button() == Qt.MouseButton.LeftButton else "right_click"
            self.mouse_event.emit({"type": "mouse", "x": xy[0], "y": xy[1], "action": button})

    def mouseMoveEvent(self, event):
        if not self.input_enabled:
            return
        xy = self._scaled_xy(event.position().toPoint())
        if xy:
            self.mouse_event.emit({"type": "mouse", "x": xy[0], "y": xy[1], "action": "move"})

    def keyPressEvent(self, event):
        if self.input_enabled:
            self.key_event.emit({"type": "key", "vk": event.nativeVirtualKey(), "action": "down"})

    def keyReleaseEvent(self, event):
        if self.input_enabled:
            self.key_event.emit({"type": "key", "vk": event.nativeVirtualKey(), "action": "up"})


class RemoteViewWidget(QWidget):
    """
    Drop this into any tab/dialog. Call connect_to(ws_url) to start
    streaming; call disconnect_stream() when the session ends (the
    caller is still responsible for calling the REST start/end/blank
    endpoints -- this widget only owns the video+input socket).
    """
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.screen_label = RemoteScreenLabel()
        layout.addWidget(self.screen_label)
        self.setLayout(layout)

        self.client = None
        self.screen_label.mouse_event.connect(self._forward)
        self.screen_label.key_event.connect(self._forward)

    def connect_to(self, ws_url: str, input_enabled: bool = True):
        self.disconnect_stream()
        self.screen_label.input_enabled = input_enabled
        self.screen_label.setText("Connecting…")
        self.client = RemoteFrameClient(ws_url)
        self.client.frame_received.connect(self._on_frame)
        self.client.resolution_received.connect(self._on_resolution)
        self.client.connection_state.connect(self._on_state)
        self.client.start()

    def disconnect_stream(self):
        if self.client:
            self.client.stop()
            self.client = None

    def _forward(self, command: dict):
        if self.client:
            self.client.send_command(command)

    def _on_resolution(self, w, h):
        self.screen_label.agent_width = w
        self.screen_label.agent_height = h

    def _on_frame(self, frame_bytes: bytes):
        img = QImage.fromData(frame_bytes)
        if img.isNull():
            return
        pixmap = QPixmap.fromImage(img).scaled(
            self.screen_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.screen_label.setPixmap(pixmap)

    def _on_state(self, state: str):
        if state == "connecting":
            self.screen_label.setText("Connecting…")
        elif state == "disconnected":
            self.screen_label.setText("Session ended.")
        elif state.startswith("error:"):
            self.screen_label.setText(f"Connection error: {state[6:]}")
