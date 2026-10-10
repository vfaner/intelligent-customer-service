"""In-memory login throttling.

The admin login used to accept unlimited attempts: with the seeded default
account (admin/123456) that is an open door, and every attempt also burns a
full PBKDF2 verification — a free CPU-exhaustion amplifier even without
guessing right.

Policy: after ``MAX_FAILURES`` failures within ``WINDOW`` seconds a key is
locked out for ``BASE_LOCKOUT`` seconds; each further violation doubles the
lockout, capped at ``MAX_LOCKOUT``. Two keys are tracked per attempt —
``ip`` alone (stops rotating usernames) and ``ip|username`` (stops one
typo-prone user behind a shared NAT from locking out the whole office…
they only lock their own pair).

In-memory and single-process by design: the app ships as one uvicorn
worker, and a multi-worker deployment gets per-worker buckets — weaker, but
still bounded. Redis is deliberately not used (it is not wired up in this
project). State resets on restart, which is acceptable for a brake, not a
ban list.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

MAX_FAILURES = 5
WINDOW = 900.0           # failures older than 15 min stop counting
BASE_LOCKOUT = 900.0     # first lockout: 15 min
MAX_LOCKOUT = 86400.0    # cap: 24 h
_MAX_KEYS = 10000        # memory brake on the tracking dict


@dataclass
class _Bucket:
    failures: list = field(default_factory=list)
    lock_until: float = 0.0
    violations: int = 0


class LoginLimiter:
    def __init__(self) -> None:
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Bound memory: drop settled buckets when the dict grows too large."""
        if len(self._buckets) <= _MAX_KEYS:
            return
        dead = [
            k for k, b in self._buckets.items()
            if b.lock_until <= now and not any(t > now - WINDOW for t in b.failures)
        ]
        for k in dead:
            del self._buckets[k]
        if len(self._buckets) > _MAX_KEYS:
            # Still over: everything left is either locked or recently active.
            # Dropping all of them merely resets the brake, never locks anyone
            # out incorrectly — the safe direction to fail.
            self._buckets.clear()

    def check(self, key: str) -> float:
        """Seconds of remaining lockout for ``key``; 0.0 means allowed."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is not None and b.lock_until > now:
                return b.lock_until - now
        return 0.0

    def record_failure(self, key: str) -> float:
        """Register a failed attempt. Returns the lockout just imposed, or 0."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            b = self._buckets.setdefault(key, _Bucket())
            b.failures = [t for t in b.failures if t > now - WINDOW]
            b.failures.append(now)
            if b.lock_until > now:
                return b.lock_until - now
            if len(b.failures) >= MAX_FAILURES:
                b.violations += 1
                lockout = min(BASE_LOCKOUT * (2 ** (b.violations - 1)), MAX_LOCKOUT)
                b.lock_until = now + lockout
                b.failures.clear()
                return lockout
        return 0.0

    def record_success(self, key: str) -> None:
        """A successful login wipes the key's history — typos don't add up."""
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        """Test helper: forget everything."""
        with self._lock:
            self._buckets.clear()


# Process-wide singleton — imported by api/auth.py.
login_limiter = LoginLimiter()
