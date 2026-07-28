import asyncio
import json
from typing import Dict, Set

from fastapi import WebSocket

from presence import r, PRESENCE_CHANNEL


class ConsoleConnectionManager:
    """Tracks connected console WebSocket clients and fans out presence updates."""

    def __init__(self):
        self.active: Set[WebSocket] = set()
        self._pubsub_task = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def start_redis_listener(self):
        """Subscribes to Redis presence_updates and rebroadcasts to all consoles."""
        from presence import _use_redis, r, PRESENCE_CHANNEL
        if not _use_redis or r is None:
            print("[ws] Skipping Redis presence listener (using in-memory mode)")
            return
        try:
            pubsub = r.pubsub()
            pubsub.subscribe(PRESENCE_CHANNEL)
            loop = asyncio.get_event_loop()

            def _reader():
                for message in pubsub.listen():
                    if message["type"] == "message":
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast(json.loads(message["data"])), loop
                        )

            loop.run_in_executor(None, _reader)
        except Exception as e:
            print(f"[ws] Redis listener disabled: {e}")


console_manager = ConsoleConnectionManager()


class RemoteSessionManager:
    """
    Relays screen frames from an agent to whichever console started the
    session, and input/blank-screen commands from console -> agent.
    One active session per employee_id at a time.
    """

    def __init__(self):
        self.agent_sockets: Dict[str, WebSocket] = {}
        self.console_sockets: Dict[str, WebSocket] = {}

    async def register_agent(self, employee_id: str, ws: WebSocket):
        await ws.accept()
        self.agent_sockets[employee_id] = ws

    async def register_console(self, employee_id: str, ws: WebSocket):
        await ws.accept()
        self.console_sockets[employee_id] = ws

    def unregister_agent(self, employee_id: str):
        self.agent_sockets.pop(employee_id, None)

    def unregister_console(self, employee_id: str):
        self.console_sockets.pop(employee_id, None)

    async def frame_to_console(self, employee_id: str, frame_bytes: bytes):
        ws = self.console_sockets.get(employee_id)
        if ws:
            await ws.send_bytes(frame_bytes)

    async def command_to_agent(self, employee_id: str, command: dict):
        ws = self.agent_sockets.get(employee_id)
        if ws:
            try:
                await ws.send_json(command)
            except Exception as e:
                print(f"[remote] failed to deliver command to agent {employee_id}: {e}")
                self.agent_sockets.pop(employee_id, None)


remote_manager = RemoteSessionManager()
