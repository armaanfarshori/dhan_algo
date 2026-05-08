"""Rolling in-memory log buffer exposed via /api/logs."""
import logging
from collections import deque
from datetime import datetime, timezone

_buffer: deque = deque(maxlen=100)

class BufferHandler(logging.Handler):
    ICONS = {
        "INFO":    "·",
        "WARNING": "⚠",
        "ERROR":   "✗",
        "CRITICAL":"⛔",
        "DEBUG":   "·",
    }
    SKIP = {"aiohttp.access", "dhan.client"}   # too noisy

    def emit(self, record):
        if record.name in self.SKIP:
            return
        _buffer.append({
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "icon":    self.ICONS.get(record.levelname, "·"),
            "name":    record.name.replace("dhan.", ""),
            "msg":     self.format(record),
        })

def install():
    h = BufferHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(h)

def get_logs(limit: int = 50) -> list:
    return list(_buffer)[-limit:]
