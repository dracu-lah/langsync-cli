import logging
import os

import pytest

from langsync import runlog


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LANGSYNC_NO_LOG", raising=False)
    monkeypatch.delenv("LANGSYNC_LOG_TEXT", raising=False)
    monkeypatch.delenv("LANGSYNC_DEBUG", raising=False)
    yield tmp_path
    runlog.stop()


def read_log(tmp_path):
    return (tmp_path / "langsync" / "langsync.log").read_text()


def flush():
    for h in logging.getLogger("langsync.run").handlers:
        h.flush()


def test_start_writes_run_start_with_run_id(tmp_path):
    rid = runlog.start(provider="google", locales=3)
    flush()
    text = read_log(tmp_path)
    assert f"[{rid}] run_start" in text
    assert "provider=google" in text and "locales=3" in text
    assert runlog.path() == str(tmp_path / "langsync" / "langsync.log")


def test_each_run_gets_its_own_id(tmp_path):
    a = runlog.start()
    b = runlog.start()
    assert a != b


def test_redacts_keys_env_values_and_home(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "sekret-value-123")
    out = runlog.redact("failed with sekret-value-123 at " + os.path.expanduser("~") + "/x")
    assert "sekret-value-123" not in out and out.endswith("~/x")
    assert "abcd1234" not in runlog.redact("api_key=abcd1234")
    assert "abcd1234" not in runlog.redact("Authorization: DeepL-Auth-Key abcd1234")
    assert "abcd1234" not in runlog.redact("https://x/y?key=abcd1234&q=1")
    assert "tokens=42" in runlog.redact("tokens=42")


def test_error_field_is_redacted_in_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "sekret-value-123")
    runlog.start()
    runlog.request("deepl", 1, 5, False, 10, status=403, error="bad sekret-value-123")
    flush()
    assert "sekret-value-123" not in read_log(tmp_path)


def test_text_never_logged_by_default(tmp_path):
    runlog.start()
    runlog.log_text("src", "Hello private world")
    flush()
    assert "Hello private world" not in read_log(tmp_path)


def test_text_logged_only_with_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSYNC_LOG_TEXT", "1")
    monkeypatch.setenv("LANGSYNC_DEBUG", "1")  # DEBUG lines are not in the file anyway
    runlog.start()
    runlog.log_text("src", "Hello private world")
    flush()
    assert "Hello private world" not in read_log(tmp_path)  # file stays INFO; text goes to stderr mirror


def test_no_log_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSYNC_NO_LOG", "1")
    runlog.start()
    runlog.info("x")
    assert runlog.path() is None and runlog.hint() is None
    assert not (tmp_path / "langsync").exists()


def test_unwritable_log_dir_does_not_break_run(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))  # a file, so makedirs fails
    runlog.start()
    runlog.info("still fine")
    runlog.request("google", 1, 2, False, 5)
    runlog.finish(0)
    assert runlog.path() is None


def test_log_calls_swallow_bad_input():
    runlog.start()
    runlog.info("x", bad=object())
    runlog.locale_done("fr", None, None, None, None, None, "not-a-number")
    runlog.exception(None)


def test_rotation_keeps_three_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runlog, "MAX_BYTES", 2000)
    runlog.start()
    for i in range(400):
        runlog.info("filler", n=i, pad="x" * 40)
    flush()
    names = sorted(p.name for p in (tmp_path / "langsync").iterdir())
    assert names == ["langsync.log", "langsync.log.1", "langsync.log.2"]


def test_request_and_slowest_five(tmp_path):
    runlog.start()
    for ms in (10, 900, 30, 700, 50, 800, 20, 600):
        runlog.request("google", 4, 120, True, ms, status=200)
    runlog.finish(0, translated=8)
    flush()
    text = read_log(tmp_path)
    assert "request provider=google texts=4 chars=120 packed=true latency_ms=900" in text
    slow = [ln for ln in text.splitlines() if " slowest " in ln]
    assert len(slow) == 5
    assert "rank=1" in slow[0] and "latency_ms=900" in slow[0]
    assert "run_end exit=0" in text and "translated=8" in text


def test_exception_traceback_goes_to_log(tmp_path):
    runlog.start()
    try:
        raise ValueError("boom")
    except ValueError as e:
        runlog.exception(e)
    flush()
    text = read_log(tmp_path)
    assert "unhandled_exception ValueError: boom" in text and "Traceback" in text


def test_hint_and_slow_hint_once(tmp_path):
    runlog.start()
    assert "langsync report" in runlog.hint()
    assert runlog.slow_hint(5) is None
    assert runlog.slow_hint(61) is not None
    assert runlog.slow_hint(120) is None


def test_report_contains_only_requested_run(tmp_path):
    first = runlog.start()
    runlog.info("marker_one")
    runlog.finish(0)
    second = runlog.start()
    runlog.info("marker_two")
    try:
        raise RuntimeError("bad")
    except RuntimeError as e:
        runlog.exception(e)
    runlog.finish(1)
    flush()
    text, removed, ids = runlog.build_report()
    assert ids == [second]
    assert "marker_two" in text and "Traceback" in text and "marker_one" not in text
    assert "langsync:" in text and "python:" in text
    text2, _, ids2 = runlog.build_report(last=2)
    assert ids2 == [first, second] and "marker_one" in text2
    text3, _, _ = runlog.build_report(run=first)
    assert "marker_one" in text3 and "marker_two" not in text3


def test_report_redacts_what_was_already_in_the_file(tmp_path, monkeypatch):
    runlog.start()
    runlog.info("x")
    flush()
    p = tmp_path / "langsync" / "langsync.log"
    rid = runlog.run_id()
    p.write_text(p.read_text() + f"2026-11-02T00:00:00 INFO  [{rid}] leak api_key=zzzz9999\n")
    text, removed, _ = runlog.build_report()
    assert "zzzz9999" not in text and removed == 1


def test_report_with_no_log_is_empty_not_error(tmp_path):
    text, removed, ids = runlog.build_report(base=str(tmp_path / "missing.log"))
    assert ids == [] and removed == 0 and "runs: none" in text


def test_slow_tick_prints_hint_once_through_progress(tmp_path, monkeypatch):
    from langsync import cli

    class FakeConsole:
        def __init__(self):
            self.lines = []

        def print(self, text, **kw):
            self.lines.append(text)

    class FakeProgress:
        console = FakeConsole()

    runlog.start()
    now = [1000.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    cli._slow_tick(FakeProgress, 1000.0)
    assert FakeProgress.console.lines == []
    now[0] = 1000.0 + runlog.SLOW_RUN_SECONDS + 1
    cli._slow_tick(FakeProgress, 1000.0)
    cli._slow_tick(FakeProgress, 1000.0)
    assert len(FakeProgress.console.lines) == 1
    assert "langsync report" in FakeProgress.console.lines[0]
