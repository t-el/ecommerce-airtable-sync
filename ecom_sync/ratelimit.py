"""Client-side token bucket so we stay under Airtable's 5 requests/second/base limit."""

from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, rate_per_sec: float, burst: int = 1,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / rate_per_sec
        self.burst = burst
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.waited = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            if self._next < now - self.interval * (self.burst - 1):
                self._next = now - self.interval * (self.burst - 1)
            wait = self._next - now
            self._next += self.interval
        if wait > 0:
            self.waited += wait
            self.sleep(wait)

    def pause(self, seconds: float) -> None:
        """After a 429, nobody sends anything until the cooldown has passed."""
        with self._lock:
            self._next = max(self._next, self.clock() + seconds)
