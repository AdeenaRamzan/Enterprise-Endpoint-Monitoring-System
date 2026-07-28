"""
Sends {employee_id, status, active_app} over the backend's
/ws/agent_heartbeat WebSocket every heartbeat_interval_seconds.
Runs in its own thread so it doesn't block the screenshot loop.
"""

import asyncio
import json
import threading

import websockets

from idle_tracking import get_status, get_active_window_title


def _ws_url_from_http(backend_url: str) -> str:
    return backend_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws/agent_heartbeat"


async def _heartbeat_loop(employee_id: str, backend_url: str, interval_seconds: int, idle_threshold_seconds: int):
    ws_url = _ws_url_from_http(backend_url)
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                while True:
                    status = get_status(idle_threshold_seconds)
                    active_app = get_active_window_title()
                    await ws.send(json.dumps({
                        "employee_id": employee_id, "status": status, "active_app": active_app,
                    }))
                    await asyncio.sleep(interval_seconds)
        except Exception as e:
            print(f"[heartbeat] connection lost ({e}), retrying in 5s")
            await asyncio.sleep(5)


def start_heartbeat_thread(employee_id: str, backend_url: str, interval_seconds: int = 7,
                            idle_threshold_seconds: int = 60):
    def _run():
        asyncio.run(_heartbeat_loop(employee_id, backend_url, interval_seconds, idle_threshold_seconds))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
