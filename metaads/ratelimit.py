"""Thread-safe sliding-window rate limiter (N calls per rolling 60 s)."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(self, per_minute: int, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.limit, self.window = per_minute, window
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a call is allowed, then record it."""
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0])
            self._sleep(max(wait, 0.05))


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def get_limiter(name: str, per_minute: int) -> RateLimiter:
    """Shared limiter per API name ('image', 'video') so every caller in the process counts together."""
    with _limiters_lock:
        if name not in _limiters:
            _limiters[name] = RateLimiter(per_minute)
        return _limiters[name]
