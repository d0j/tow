"""Login throttling for the LAN password (in-process, per client address and for all of them)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class LoginThrottle:
    """Lock a client out after repeated failures, doubling the pause each time.

    ``max_failures`` failures inside ``window_sec`` start a lockout of ``base_delay_sec``
    that doubles with every further failure up to ``max_delay_sec``. A success resets
    the client. Failures from all addresses together have their own budget
    (``global_max_failures``, pauses up to ``global_max_delay_sec``): many addresses
    guessing a little each cannot add up to many guesses.

    ``attempt`` reserves a try atomically and counts it as a failure right away
    (``success`` gives it back), so parallel wrong guesses see each other instead of
    all passing the check before the first failure is recorded.
    ``verification_slots`` bounds concurrent password checks, which are
    deliberately expensive (PBKDF2), so failed guesses cannot exhaust the CPU.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_sec: float = 600.0,
        base_delay_sec: float = 30.0,
        max_delay_sec: float = 3600.0,
        global_max_failures: int = 30,
        global_max_delay_sec: float = 900.0,
        verification_slots: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.base_delay_sec = base_delay_sec
        self.max_delay_sec = max_delay_sec
        self.global_max_failures = global_max_failures
        self.global_max_delay_sec = global_max_delay_sec
        self.verification = threading.BoundedSemaphore(verification_slots)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._all_failures: list[tuple[float, str]] = []
        self._all_locked_until = 0.0

    def _wait_locked(self, client: str, now: float) -> int:
        remaining = max(self._locked_until.get(client, 0.0), self._all_locked_until) - now
        return max(0, int(remaining + 0.999))

    def retry_after(self, client: str) -> int:
        """Seconds the client must still wait; 0 when it may try."""
        with self._lock:
            return self._wait_locked(client, self._clock())

    def attempt(self, client: str) -> int:
        """Reserve one try: the seconds to wait (nothing reserved), or 0 - the try is counted
        as a failure now and given back by ``success``."""
        now = self._clock()
        with self._lock:
            wait = self._wait_locked(client, now)
            if wait:
                return wait
            self._count_failure(client, now)
            return 0

    def failure(self, client: str) -> None:
        with self._lock:
            self._count_failure(client, self._clock())

    def _count_failure(self, client: str, now: float) -> None:
        recent = [ts for ts in self._failures.get(client, []) if now - ts < self.window_sec]
        recent.append(now)
        self._failures[client] = recent
        excess = len(recent) - self.max_failures
        if excess >= 0:
            self._locked_until[client] = now + min(self.max_delay_sec, self.base_delay_sec * 2**excess)
        self._all_failures = [(ts, c) for ts, c in self._all_failures if now - ts < self.window_sec]
        self._all_failures.append((now, client))
        excess = len(self._all_failures) - self.global_max_failures
        if excess >= 0:
            delay = min(self.global_max_delay_sec, self.base_delay_sec * 2**excess)
            self._all_locked_until = max(self._all_locked_until, now + delay)

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
            self._locked_until.pop(client, None)
            self._all_failures = [(ts, c) for ts, c in self._all_failures if c != client]
            if len(self._all_failures) < self.global_max_failures:
                self._all_locked_until = 0.0
