import json
import time

from config import settings

_in_memory_presence = {}
_use_redis = False
r = None

try:
    import redis
    _client = redis.from_url(settings.redis_url, socket_timeout=1.0, decode_responses=True)
    _client.ping()
    r = _client
    _use_redis = True
except Exception:
    print("[Redis] Redis server not reachable. Falling back to in-memory presence tracking.")
    _use_redis = False

PRESENCE_KEY = "presence:{employee_id}"
PRESENCE_CHANNEL = "presence_updates"
PRESENCE_TTL_SECONDS = 30  # if no heartbeat in this window, agent is considered offline


def update_presence(employee_id: str, status: str, active_app: str = None):
    """status: 'active' | 'idle'. Called on every agent heartbeat."""
    previous = get_presence(employee_id)
    if status == "idle":
        idle_since = previous.get("idle_since") if previous.get("status") == "idle" else time.time()
    else:
        idle_since = None

    payload = {
        "employee_id": employee_id,
        "status": status,
        "active_app": active_app,
        "last_seen": time.time(),
        "idle_since": idle_since,
    }
    if _use_redis and r is not None:
        try:
            r.set(PRESENCE_KEY.format(employee_id=employee_id), json.dumps(payload), ex=PRESENCE_TTL_SECONDS)
            r.publish(PRESENCE_CHANNEL, json.dumps(payload))
        except Exception:
            _in_memory_presence[employee_id] = payload
    else:
        _in_memory_presence[employee_id] = payload
    return payload


def get_presence(employee_id: str):
    if _use_redis and r is not None:
        try:
            raw = r.get(PRESENCE_KEY.format(employee_id=employee_id))
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    data = _in_memory_presence.get(employee_id)
    if not data:
        return {"employee_id": employee_id, "status": "offline", "last_seen": None}

    if time.time() - data.get("last_seen", 0) > PRESENCE_TTL_SECONDS:
        return {"employee_id": employee_id, "status": "offline", "last_seen": None}
    return data


def get_all_presence(employee_ids):
    return [get_presence(eid) for eid in employee_ids]

