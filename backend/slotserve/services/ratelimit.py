"""In-memory throttle for failed sign-in and sign-up attempts."""

import math
import time
from collections import deque

from fastapi import status

from ..errors import ApiError

WINDOW_SECONDS = 15 * 60
MAX_PER_USERNAME = 10
MAX_PER_IP = 30


class AttemptLimiter:
    def __init__(self, window: float = WINDOW_SECONDS):
        self.window = window
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    @staticmethod
    def _keys(username: str | None, ip: str | None) -> list[tuple[str, int]]:
        keys = []
        if username:
            keys.append((f"u:{username.lower()}", MAX_PER_USERNAME))
        if ip:
            keys.append((f"ip:{ip}", MAX_PER_IP))
        return keys

    def check(self, *, username: str | None = None, ip: str | None = None) -> None:
        """Raises 429 while either the account or the address is locked out."""
        now = time.monotonic()
        for key, limit in self._keys(username, ip):
            q = self._recent(key, now)
            if len(q) >= limit:
                wait = q[0] + self.window - now
                raise ApiError(
                    status.HTTP_429_TOO_MANY_REQUESTS, "too_many_attempts", minutes=max(1, math.ceil(wait / 60))
                )

    def failure(self, *, username: str | None = None, ip: str | None = None) -> None:
        now = time.monotonic()
        for key, _ in self._keys(username, ip):
            self._recent(key, now).append(now)
        if len(self._failures) > 10_000:  # forget stale keys
            for key in [k for k, q in self._failures.items() if not q or q[-1] <= now - self.window]:
                del self._failures[key]

    def success(self, *, username: str) -> None:
        self._failures.pop(f"u:{username.lower()}", None)
