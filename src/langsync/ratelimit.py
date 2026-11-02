"""One process-wide governor for every translator request.

Locales and batches run on many threads; without a shared gate each thread
hammers the provider on its own. The governor gives them:

* a token bucket (requests per second) and an in-flight cap,
* one shared pause: a rate limit seen by any thread holds back all threads,
  with exponential backoff, jitter, a cap, and the server's Retry-After,
* a circuit breaker that stops the run after N straight failures.

Time and randomness are looked up through module attributes so tests can swap
in a fake clock.
"""
import random
import threading
import time
from email.utils import parsedate_to_datetime

from . import runlog

# Kinds of failure that mean "the service is unhappy", not "this item is bad".
SERVICE_KINDS = ("rate_limit", "network")


class BreakerOpen(Exception):
    """The run was aborted because the translator kept refusing requests."""


def parse_retry_after(value, now=None, cap=300.0):
    """Seconds to wait from a Retry-After header (seconds or HTTP date).
    Returns None when missing or unusable, never more than `cap`."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        ref = time.time() if now is None else now
        seconds = when.timestamp() - ref
    if seconds != seconds or seconds < 0:  # NaN or past date
        return 0.0
    return min(seconds, cap)


def backoff_delay(failures, base=1.0, cap=60.0, jitter=0.25, rnd=None):
    """1, 2, 4, 8 ... seconds, capped, with +/- jitter (a fraction)."""
    raw = min(cap, base * (2 ** max(0, failures - 1)))
    r = (rnd or random.random)()
    return min(cap, raw * (1 + jitter * (2 * r - 1)))


class Governor:
    def __init__(self, rate=4.0, burst=None, max_in_flight=None, backoff_base=1.0,
                 backoff_max=60.0, breaker_threshold=8, retry_after_cap=300.0,
                 rnd=None):
        self.rate = float(rate) if rate else 0.0  # 0 disables pacing
        self.capacity = float(burst or max(1.0, self.rate))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.threshold = breaker_threshold  # 0 disables the breaker
        self.retry_after_cap = retry_after_cap
        self._rnd = rnd
        self._cond = threading.Condition()
        self._tokens = self.capacity
        self._stamp = time.monotonic()
        self._pause_until = 0.0
        self._slots = max_in_flight if max_in_flight else None
        self._in_flight = 0
        self.failures = 0  # consecutive service failures
        self.min_scopes = 1  # distinct locales that must fail in the streak
        self._scopes = set()
        self.total_failures = 0
        self.open_reason = None
        self.last_message = None
        self.blocked = False  # provider answered with a hard block (captcha)

    # -- gate ---------------------------------------------------------------
    def _refill(self, now):
        if self.rate:
            self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    def acquire(self):
        """Block until a request may go out. Raises BreakerOpen if aborted."""
        with self._cond:
            while True:
                if self.open_reason:
                    raise BreakerOpen(self.open_reason)
                now = time.monotonic()
                self._refill(now)
                wait = 0.0
                if now < self._pause_until:
                    wait = self._pause_until - now
                elif self._slots is not None and self._in_flight >= self._slots:
                    wait = 0.05
                elif self.rate and self._tokens < 1.0 - 1e-9:
                    wait = max(1e-3, (1.0 - self._tokens) / self.rate)
                else:
                    if self.rate:
                        self._tokens -= 1.0
                    self._in_flight += 1
                    return
                # Sleep outside the lock so other threads can see the state.
                self._cond.release()
                try:
                    time.sleep(wait)
                finally:
                    self._cond.acquire()

    def release(self):
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)

    # -- outcomes -----------------------------------------------------------
    def success(self):
        with self._cond:
            self.failures = 0
            self._scopes.clear()

    def penalize(self, kind, message, retry_after=None, hard=False, scope=None):
        """Record a service failure: pause everyone, maybe open the breaker."""
        with self._cond:
            self.failures += 1
            self.total_failures += 1
            self._scopes.add(scope)
            self.last_message = message
            wait = backoff_delay(self.failures, self.backoff_base, self.backoff_max,
                                 rnd=self._rnd)
            if retry_after:
                wait = max(wait, min(retry_after, self.retry_after_cap))
            self._pause_until = max(self._pause_until, time.monotonic() + wait)
            runlog.governor_wait(kind, wait, f"failure {self.failures}"
                                 + (f", retry-after {retry_after}" if retry_after else ""))
            if hard:
                self.blocked = True
            if self.threshold and (hard or (self.failures >= self.threshold
                                            and (len(self._scopes) >= self.min_scopes
                                                 or self.failures >= 2 * self.threshold))):
                what = "blocked the client" if hard else "refused or failed"
                self.open_reason = (
                    f"run aborted: translator {what} {self.failures} "
                    f"request{'s' if self.failures != 1 else ''} in a row ({kind})"
                )
                runlog.governor_open(self.open_reason)

    def call(self, fn, classify, scope=None):
        """Run one provider request through the gate. `classify(exc)` returns
        (kind, retry_after_seconds_or_None, hard_block)."""
        self.acquire()
        try:
            result = fn()
        except Exception as exc:
            kind, retry_after, hard = classify(exc)
            if kind in SERVICE_KINDS:
                self.penalize(kind, str(exc), retry_after, hard, scope)
            raise
        else:
            self.success()
            return result
        finally:
            self.release()


# The governor for the current run. cli.main installs one; library users and
# tests that never do get a pass-through (no pacing, no breaker).
_current = None


def install(governor):
    global _current
    _current = governor


def current():
    return _current
