"""Simple in-process sliding-window rate limiter for failed login attempts.

Tracks failed attempts per ``(client IP, normalized email)`` pair. Keys:

- At most ``MAX_FAILED_ATTEMPTS`` failures within ``WINDOW_SECONDS`` is
  allowed; further attempts are rejected with HTTP 429 and a ``Retry-After``
  header for the remainder of the window.
- Only *failed* attempts are counted; the window resets on a successful
  login.

Scope caveat: the counters live in this process's memory (a module-level
dict guarded by a lock). That is fine for the single-process MVP, but
multi-process/multi-replica deployments need a shared store (e.g. Redis) —
each worker would otherwise have its own allowance and an attacker could
multiply the effective limit by the worker count.
"""
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

MAX_FAILED_ATTEMPTS = 5
WINDOW_SECONDS = 300.0  # 5 minutes

_FailedKey = Tuple[str, str]

# (ip, email-normalized) -> deque of monotonic timestamps of failed attempts
_failed_attempts: Dict[_FailedKey, Deque[float]] = {}
_lock = threading.Lock()


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def check_rate_limit(ip: str, email: str) -> Optional[int]:
    """Return the ``Retry-After`` seconds when the key is blocked, else ``None``."""
    key = (ip, _normalize(email))
    now = time.monotonic()
    with _lock:
        attempts = _failed_attempts.get(key)
        if attempts is None:
            return None
        while attempts and now - attempts[0] >= WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) < MAX_FAILED_ATTEMPTS:
            return None
        retry_after = attempts[0] + WINDOW_SECONDS - now
        return max(1, int(retry_after) + (1 if retry_after % 1 > 0 else 0))


def record_failed_attempt(ip: str, email: str) -> None:
    """Count one failed login attempt for the key."""
    key = (ip, _normalize(email))
    now = time.monotonic()
    with _lock:
        attempts = _failed_attempts.setdefault(key, deque())
        attempts.append(now)
        while attempts and now - attempts[0] >= WINDOW_SECONDS:
            attempts.popleft()


def reset_rate_limit(ip: str, email: str) -> None:
    """Clear the key's counter (used after a successful login)."""
    with _lock:
        _failed_attempts.pop((ip, _normalize(email)), None)
