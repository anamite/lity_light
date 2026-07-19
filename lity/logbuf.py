"""In-memory ring buffer of recent log lines + bus events, for the dashboard's
Advanced tab live-log window. Nothing here is persisted — it's a quick peek."""

import collections
import itertools
import logging
import time


class LogBuffer:
    def __init__(self, maxlen: int = 600):
        self.buf = collections.deque(maxlen=maxlen)
        self._ids = itertools.count(1)

    def add(self, level: str, source: str, message: str):
        self.buf.append({
            "id": next(self._ids),
            "ts": time.strftime("%H:%M:%S", time.gmtime()),
            "level": level,
            "source": source,
            "message": str(message)[:500],
        })

    def since(self, last_id: int) -> list[dict]:
        return [r for r in self.buf if r["id"] > last_id]


class BufferHandler(logging.Handler):
    def __init__(self, logbuf: LogBuffer):
        super().__init__(logging.INFO)
        self.logbuf = logbuf

    def emit(self, record: logging.LogRecord):
        try:
            self.logbuf.add(record.levelname, record.name, record.getMessage())
        except Exception:
            pass


def attach(logbuf: LogBuffer):
    """Capture everything the lity.* loggers say (INFO and up)."""
    lg = logging.getLogger("lity")
    if lg.level in (logging.NOTSET,) or lg.level > logging.INFO:
        lg.setLevel(logging.INFO)
    lg.addHandler(BufferHandler(logbuf))
