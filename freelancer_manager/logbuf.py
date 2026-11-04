"""In-memory log ring so /menu can show the last lines without shelling out.

A container or a systemd unit without journal access can still tell you what the bot
saw an hour ago.
"""

from __future__ import annotations

import collections
import logging

LINES: collections.deque[str] = collections.deque(maxlen=80)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LINES.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def install(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h, RingHandler) for h in root.handlers):
        return
    h = RingHandler()
    h.setLevel(level)
    h.setFormatter(logging.Formatter("%H:%M:%S %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)


def tail(n: int = 12) -> list[str]:
    return list(LINES)[-n:]
