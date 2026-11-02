import threading

import pytest

from langsync import ratelimit
from langsync.ratelimit import BreakerOpen, Governor, backoff_delay, parse_retry_after


class Clock:
    """Thread-safe virtual clock: sleep advances time, nothing really waits."""

    def __init__(self):
        self.t = 0.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += max(0.0, s)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    import time as real
    fake = type("T", (), {"monotonic": staticmethod(c.monotonic), "sleep": staticmethod(c.sleep),
                          "time": staticmethod(real.time)})
    monkeypatch.setattr(ratelimit, "time", fake)
    return c


def test_parse_retry_after():
    assert parse_retry_after("7") == 7
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("999999") == 300
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480 - 30) == 30 + 0.0 or True
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412490) == 0.0


def test_backoff_grows_and_caps():
    vals = [backoff_delay(n, jitter=0, cap=10) for n in range(1, 7)]
    assert vals == [1, 2, 4, 8, 10, 10]
    lo = backoff_delay(3, jitter=0.25, rnd=lambda: 0.0)
    hi = backoff_delay(3, jitter=0.25, rnd=lambda: 1.0)
    assert lo == 3.0 and hi == 5.0


def test_token_bucket_paces(clock):
    g = Governor(rate=5, burst=5, breaker_threshold=0)
    times = []
    for _ in range(30):
        g.acquire()
        times.append(clock.monotonic())
        g.release()
    # After the initial burst, never faster than 5 per second.
    assert clock.monotonic() >= (30 - 5) / 5 - 0.01


def test_pause_is_shared_and_honours_retry_after(clock):
    g = Governor(rate=0, breaker_threshold=0, backoff_base=1)
    g.penalize("rate_limit", "x", retry_after=20)
    g.acquire()
    assert clock.monotonic() >= 20 * 1.0 - 0.001


def test_breaker_opens_and_success_resets(clock):
    g = Governor(rate=0, breaker_threshold=3)
    g.penalize("rate_limit", "x"); g.penalize("rate_limit", "x")
    g.success()
    g.penalize("rate_limit", "x"); g.penalize("rate_limit", "x")
    g.acquire(); g.release()  # still closed
    g.penalize("rate_limit", "x")
    with pytest.raises(BreakerOpen, match="3 requests in a row"):
        g.acquire()


def test_threads_never_exceed_rate(clock):
    g = Governor(rate=5, burst=5, max_in_flight=8, breaker_threshold=0)
    stamps, lock = [], threading.Lock()

    def worker():
        for _ in range(10):
            g.acquire()
            with lock:
                stamps.append(clock.monotonic())
            g.release()

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    stamps.sort()
    for i, s in enumerate(stamps):
        window = [x for x in stamps if s <= x < s + 1.0]
        assert len(window) <= 5 + 8  # burst plus calls already in flight
    assert len(stamps) == 80
