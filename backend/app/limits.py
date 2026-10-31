"""Limits that keep a small public server usable: how often one visitor may upload, change
and look at pages, and how many heavy jobs (changes, uploads) run at once. Anyone with the
link can use the site, so one person (or a script) mustn't be able to wear it out.

Visitors are told by their address, as the proxy in front of the server passes it on
(uvicorn --proxy-headers). Counts are kept in memory: a restart forgets them, which is fine.
"""

import asyncio
import re
import threading
import time
from collections import deque

from . import config

_DOC = r"/api/documents/[^/]+"
# (kind, method, path pattern): which requests count against which limit.
_KINDS = [
    ("upload", "POST", re.compile(r"^/api/documents/?$")),
    ("change", "POST", re.compile(rf"^{_DOC}/(edits|additions|deletions|undo|redo|versions/\d+|images/(delete|replace|add|move)|rows/(add|delete|resize)|columns/(add|delete|resize)|tables/scale|moves)$")),
    ("read", "POST", re.compile(rf"^{_DOC}/moves/preview$")),  # a page drawn: as often as pages are looked at
    ("export", "GET", re.compile(rf"^{_DOC}/export$")),
    ("read", "GET", re.compile(rf"^{_DOC}(/.*)?$")),
]
HEAVY = {"upload", "change", "export"}


def kind_of(method: str, path: str) -> str | None:
    for kind, m, pattern in _KINDS:
        if method == m and pattern.match(path):
            return kind
    return None


class RateLimiter:
    """At most `limit` requests of a kind per visitor in each `window` seconds."""

    def __init__(self):
        self._seen: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    @staticmethod
    def rule(kind: str) -> tuple[int, float]:
        return {
            "upload": (config.RATE_UPLOADS_PER_HOUR, 3600.0),
            "change": (config.RATE_CHANGES_PER_HOUR, 3600.0),
            "export": (config.RATE_EXPORTS_PER_HOUR, 3600.0),
            "read": (config.RATE_READS_PER_MINUTE, 60.0),
        }[kind]

    def allow(self, visitor: str, kind: str, now: float | None = None) -> bool:
        limit, window = self.rule(kind)
        now = time.monotonic() if now is None else now
        with self._lock:
            seen = self._seen.setdefault((visitor, kind), deque())
            while seen and seen[0] <= now - window:
                seen.popleft()
            if len(seen) >= limit:
                return False
            seen.append(now)
            if len(self._seen) > 10_000:  # forget visitors long gone
                for key in [k for k, d in self._seen.items() if not d or d[-1] <= now - 3600]:
                    del self._seen[key]
            return True


MESSAGES = {
    "upload": "Too many files opened in a short time. Wait a while and try again.",
    "change": "Too many changes in a short time. Wait a few minutes and try again.",
    "export": "Too many downloads in a short time. Wait a few minutes and try again.",
    "read": "Too many requests in a short time. Wait a minute and try again.",
}

limiter = RateLimiter()
_gates: dict[int, asyncio.Semaphore] = {}


def heavy_gate() -> asyncio.Semaphore:
    """Heavy jobs at once: more wait their turn rather than slow everyone down. One gate
    per event loop (tests start several)."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(id(loop))
    if gate is None:
        gate = _gates[id(loop)] = asyncio.Semaphore(config.HEAVY_JOBS)
    return gate
