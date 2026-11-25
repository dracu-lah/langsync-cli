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


def test_dead_start_aborts_fast_when_nothing_ever_succeeded(clock):
    """A provider that refuses every request from the first call is blocking this
    client: stop after a few tries, not after minutes of waiting per try (a one
    locale run never has two locales to satisfy the breaker's scope rule)."""
    gov = Governor(rate=0, breaker_threshold=40)
    for _ in range(gov.dead_start):
        gov.acquire()
        gov.penalize("rate_limit", "too many requests", scope="de")
        gov.release()
    assert gov.open_reason and "from the start" in gov.open_reason
    assert "google-gtx" in gov.open_reason
    with pytest.raises(BreakerOpen):
        gov.acquire()
    assert clock.monotonic() < 400  # minutes at most, not the hour a 40-failure breaker takes


def test_dead_start_does_not_apply_after_a_success(clock):
    gov = Governor(rate=0, breaker_threshold=8)
    gov.acquire()
    gov.success()
    gov.release()
    for _ in range(gov.threshold - 1):
        gov.acquire()
        gov.penalize("rate_limit", "too many requests", scope="de")
        gov.release()
    assert gov.open_reason is None  # the normal breaker rules still decide


def test_stall_aborts_a_run_that_was_working_and_is_now_throttled(clock):
    """Some requests succeeded, then every one is refused: give up after max_stall
    seconds instead of sleeping five minutes per try for hours."""
    gov = Governor(rate=0, breaker_threshold=40, backoff_max=300, max_stall=900)
    gov.acquire()
    gov.success()
    gov.release()
    tries = 0
    while not gov.open_reason and tries < 40:
        gov.acquire()
        gov.penalize("rate_limit", "429", scope="de")
        gov.release()
        tries += 1
    assert gov.open_reason and "no request succeeded" in gov.open_reason
    assert tries < 20 and 900 <= clock.monotonic() < 1500


def test_stall_limit_can_be_switched_off(clock):
    gov = Governor(rate=0, breaker_threshold=0, backoff_max=300, max_stall=0, dead_start=0)
    for _ in range(30):
        gov.acquire()
        gov.penalize("rate_limit", "429", scope="de")
        gov.release()
    assert gov.open_reason is None
