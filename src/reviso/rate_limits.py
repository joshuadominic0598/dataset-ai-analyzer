"""Shared cloud-call pacing from provider headers, with bounded retry waits."""

import hashlib
import math
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

MAX_ATTEMPTS = 4
WAIT_BUDGET = 180
RATE_HEADERS = tuple(
    f"x-ratelimit-{kind}-{unit}"
    for kind in ("limit", "remaining", "reset")
    for unit in ("requests", "tokens")
)

class RateLimitError(ValueError):
    pass

def seconds(value):
    try:
        number = float(value)
        return max(number, 0) if math.isfinite(number) else None
    except (TypeError, ValueError):
        pass
    if not isinstance(value, str):
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value)
    if parts and "".join(number + unit for number, unit in parts) == value:
        return sum(
            float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            for number, unit in parts
        )
    try:
        return max(parsedate_to_datetime(value).timestamp() - time.time(), 0)
    except (TypeError, ValueError, OverflowError):
        return None

class RateGate:
    def __init__(self, interval):
        self.lock = threading.Lock()
        self.interval = interval
        self.next_start = 0
        self.cooldown = 0
        self.budgets = {}

    def update(self, headers):
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        for unit in ("requests", "tokens"):
            remaining = seconds(headers.get(f"x-ratelimit-remaining-{unit}"))
            reset = seconds(headers.get(f"x-ratelimit-reset-{unit}"))
            limit = seconds(headers.get(f"x-ratelimit-limit-{unit}"))
            if remaining is not None and reset is not None:
                self.budgets[unit] = {
                    "remaining": remaining,
                    "limit": limit,
                    "reset_at": time.monotonic() + reset,
                }

    def defer(self, headers, attempt):
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        delay = seconds(headers.get("retry-after"))
        if delay is None:
            delay = 15 * 2**attempt
        self.cooldown = max(self.cooldown, time.monotonic() + delay + 0.25)

    def wait(self, estimated_tokens, deadline, notify):
        now = time.monotonic()
        until = max(self.next_start, self.cooldown)
        reason = "retry" if self.cooldown > now else "pacing"
        for unit, budget in self.budgets.items():
            if budget["reset_at"] <= now:
                continue
            needed = 1 if unit == "requests" else estimated_tokens
            if budget["limit"] is not None:
                needed = min(needed, budget["limit"])
            if budget["remaining"] < needed and budget["reset_at"] > until:
                until = budget["reset_at"] + 0.25
                reason = "provider allowance"
        delay = max(until - now, 0)
        if now >= deadline or until > deadline:
            raise RateLimitError(
                "The provider allowance needs a longer wait than this run permits. No early retry was sent. Try again later or use a smaller context or another connection."
            )
        if delay:
            if notify:
                notify("rate_limit_wait", {"seconds": round(delay, 2), "reason": reason})
            time.sleep(delay)
        self.next_start = time.monotonic() + self.interval

class RateLimiter:
    def __init__(self, interval=2):
        self.interval = interval
        self.lock = threading.Lock()
        self.gates = {}

    @contextmanager
    def guard(self, provider, key, model, deadline, notify=None, paid=False):
        identity = (provider, hashlib.sha256(key.encode()).hexdigest())
        with self.lock:
            gate = self.gates.setdefault(identity, RateGate(0 if paid else self.interval))
        if gate.lock.locked() and notify:
            notify("rate_limit_wait", {"reason": "connection queue", "seconds": None})
        if not gate.lock.acquire(timeout=max(deadline - time.monotonic(), 0)):
            raise RateLimitError(
                "This connection's queue exceeded the waiting budget. Try again when its active request finishes."
            )
        try:
            gate.interval = 0 if paid else self.interval
            yield gate
        finally:
            gate.lock.release()

RATE_LIMITER = RateLimiter()
