"""End to end: 20 locales x 134 keys against a fake translator that rate limits."""
import json
import threading

import pytest
from click.testing import CliRunner
from deep_translator.exceptions import TooManyRequests

from langsync import cli, ratelimit, translator

LOCALES = ["ar", "bn", "de", "es-419", "fa", "fr", "hi", "id", "it", "ja", "ko", "ml",
           "pl", "pt-BR", "ru", "tr", "uk", "vi", "zh-CN", "zh-TW"]


class VClock:
    def __init__(self):
        self.t = 0.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += max(0.0, s)


class FakeApi:
    def __init__(self, clock, mode="bucket", rate=5, fail_until=0, bad_langs=()):
        self.clock, self.mode, self.rate = clock, mode, rate
        self.fail_until, self.bad_langs = fail_until, set(bad_langs)
        self.tokens, self.stamp = float(rate), 0.0
        self.calls, self.lock = [], threading.Lock()

    def hit(self, kind, n, lang):
        with self.lock:
            now = self.clock.t
            self.calls.append((now, kind, n))
            if lang in self.bad_langs or self.mode == "always" or now < self.fail_until:
                self.rejected = getattr(self, "rejected", 0) + 1
                raise TooManyRequests()
            self.tokens = min(self.rate, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens < 1:
                self.rejected = getattr(self, "rejected", 0) + 1
                raise TooManyRequests()
            self.tokens -= 1


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = VClock()
    import time as real
    fake = type("T", (), {"monotonic": staticmethod(clock.monotonic), "sleep": staticmethod(clock.sleep),
                          "time": staticmethod(real.time)})
    for mod in (cli, ratelimit, translator):
        monkeypatch.setattr(mod, "time", fake)
    src = {f"k{i}": f"Text number {i}" for i in range(134)}
    (tmp_path / "en.json").write_text(json.dumps(src))
    d = tmp_path / "i18n"
    d.mkdir()
    for loc in LOCALES:
        (d / f"{loc}.json").write_text("{}")
    (tmp_path / "cfg.json").write_text(json.dumps({
        "source": str(tmp_path / "en.json"), "dir": str(d),
        "state_file": str(tmp_path / "state.json"),
        "max_parallel_locales": 4, "batch_size": 25,
    }))

    def run(api, *extra):
        class FakeGT:
            def __init__(self, source="en", target="de", **kw):
                self.target = target

            def translate_batch(self, texts):
                api.hit("batch", len(texts), self.target)
                return [f"[{self.target}] {t}" for t in texts]

            def translate(self, text):
                lines = text.split("\n")
                api.hit("req", len(lines), self.target)
                return "\n".join(f"[{self.target}] {t}" for t in lines)

        monkeypatch.setattr(translator, "GoogleTranslator", FakeGT)
        return CliRunner().invoke(cli.main, ["-c", str(tmp_path / "cfg.json"), "--rewrite", *extra])

    return clock, run, d, tmp_path


def test_token_bucket_run_completes(env):
    clock, run, d, _ = env
    api = FakeApi(clock, rate=5)
    res = run(api)
    assert res.exit_code == 0, res.output
    assert all(len(json.loads((d / f"{l}.json").read_text())) == 134 for l in LOCALES)
    assert max(n for _, _, n in api.calls) > 1  # strings are packed into requests
    assert len(api.calls) < 20 * 134 / 10  # far fewer requests than strings
    assert getattr(api, "rejected", 0) <= 0.05 * len(api.calls)


def test_always_failing_aborts_early_with_code_3(env):
    clock, run, d, tmp = env
    cfg = json.loads((tmp / "cfg.json").read_text())
    cfg.update(breaker_threshold=8, backoff_max=60, service_retries=6, max_parallel_locales=4)
    (tmp / "cfg.json").write_text(json.dumps(cfg))
    api = FakeApi(clock, mode="always")
    res = run(api)
    assert res.exit_code == 3, res.output
    # Bounded by the 8 worker threads x 6 service retries; how fast it trips inside
    # that bound depends on thread scheduling (the breaker waits for 3 locales).
    assert len(api.calls) <= 8 * 6
    assert clock.t < 900
    assert all(json.loads((d / f"{l}.json").read_text()) == {} for l in LOCALES)
    assert not (tmp / "state.json").exists()
    assert "run aborted" in res.output


def test_recovering_api_completes(env):
    clock, run, d, _ = env
    api = FakeApi(clock, fail_until=20, rate=50)
    res = run(api)
    assert res.exit_code == 0, res.output
    assert all(len(json.loads((d / f"{l}.json").read_text())) == 134 for l in LOCALES)


def test_partial_failure_exit_1_and_good_locales_written(env):
    clock, run, d, tmp = env
    cfg = json.loads((tmp / "cfg.json").read_text())
    cfg.update(breaker_threshold=0)  # this test is about isolation, not the breaker
    (tmp / "cfg.json").write_text(json.dumps(cfg))
    api = FakeApi(clock, rate=500, bad_langs=["de", "ja"])
    res = run(api, "--locales", ",".join(LOCALES))
    assert res.exit_code == 1, res.output
    good = [l for l in LOCALES if l not in ("de", "ja")]
    if res.exit_code == 1:
        assert all(len(json.loads((d / f"{l}.json").read_text())) == 134 for l in good)
    assert json.loads((d / "de.json").read_text()) == {}
