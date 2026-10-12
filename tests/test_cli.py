"""End-to-end CLI runs against a fake translator (no network)."""

import json
import os

import pytest
from click.testing import CliRunner

from langsync import cli


class FakeService:
    """Uppercases text and tags it with the target language, so tests can see
    exactly which strings went through translation."""

    instances = []

    def __init__(self, source_lang="auto", target_lang="en", **kwargs):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.kwargs = kwargs
        FakeService.instances.append(self)

    def _t(self, text):
        return f"{self.target_lang}:{text}"

    def translate_batch(self, texts, delay=0):
        return [self._t(t) for t in texts]

    def translate_one(self, text, delay=0):
        return self._t(text)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGSYNC_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(cli, "TranslationService", FakeService)
    monkeypatch.setattr(cli, "is_inside_git_repo", lambda *a, **k: False)
    FakeService.instances = []
    (tmp_path / "messages").mkdir()
    return tmp_path


def write(path, data, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(*args):
    return CliRunner().invoke(cli.main, ["--yes", *args], catch_exceptions=False)


def flat_args():
    return ["--source", "messages/en.json", "--dir", "messages"]


def test_flags_skip_the_config_prompt_and_translate_missing_keys(project):
    write(project / "messages/en.json", {"a": "Hello", "n": 3})
    write(project / "messages/fr.json", {})
    result = run(*flat_args())
    assert result.exit_code == 0, result.output
    assert read(project / "messages/fr.json") == {"a": "fr:Hello", "n": 3}
    # The source language is taken from the source file name, not hard-coded.
    assert FakeService.instances[0].source_lang == "en"


def test_no_config_non_interactive_fails_without_prompting(project):
    write(project / "messages/en.json", {"a": "Hello"})
    result = run()
    assert result.exit_code == 1
    assert "No langsync.json found" in result.output


def test_add_creates_new_locale_files(project):
    write(project / "messages/en.json", {"a": "Hello"})
    result = run(*flat_args(), "--add", "de,ja")
    assert result.exit_code == 0, result.output
    assert read(project / "messages/de.json") == {"a": "de:Hello"}
    assert read(project / "messages/ja.json") == {"a": "ja:Hello"}


def test_add_rejects_non_locale_codes(project):
    write(project / "messages/en.json", {"a": "Hello"})
    result = run(*flat_args(), "--add", "not a locale")
    assert result.exit_code == 2


def test_locale_subset_run_does_not_hide_edits_from_other_locales(project):
    write(project / "messages/en.json", {"a": "Hello"})
    write(project / "messages/fr.json", {})
    write(project / "messages/de.json", {})
    assert run(*flat_args()).exit_code == 0

    write(project / "messages/en.json", {"a": "Hi"})
    assert run(*flat_args(), "-l", "fr").exit_code == 0
    assert read(project / "messages/fr.json") == {"a": "fr:Hi"}
    assert read(project / "messages/de.json") == {"a": "de:Hello"}

    # de must still see the edit even though fr already synced it.
    check = run(*flat_args(), "--check")
    assert check.exit_code == 1
    assert run(*flat_args()).exit_code == 0
    assert read(project / "messages/de.json") == {"a": "de:Hi"}
    assert run(*flat_args(), "--check").exit_code == 0


def test_target_indentation_is_preserved(project):
    write(project / "messages/en.json", {"a": "Hello", "b": "World"})
    write(project / "messages/fr.json", {"a": "Bonjour"}, indent=4)
    assert run(*flat_args()).exit_code == 0
    text = (project / "messages/fr.json").read_text()
    assert '\n    "b": "fr:World"' in text


def test_empty_target_file_is_treated_as_empty_object(project):
    write(project / "messages/en.json", {"a": "Hello"})
    (project / "messages/fr.json").write_text("")
    result = run(*flat_args())
    assert result.exit_code == 0, result.output
    assert read(project / "messages/fr.json") == {"a": "fr:Hello"}


def test_unchanged_file_is_not_rewritten(project):
    write(project / "messages/en.json", {"a": "Hello"})
    write(project / "messages/fr.json", {})
    assert run(*flat_args()).exit_code == 0
    target = project / "messages/fr.json"
    os.utime(target, (1, 1))
    assert run(*flat_args()).exit_code == 0
    assert os.stat(target).st_mtime == 1


def test_key_order_source(project):
    (project / "langsync.json").write_text(json.dumps({
        "source": "messages/en.json", "dir": "messages", "key_order": "source",
    }))
    write(project / "messages/en.json", {"a": "A", "b": "B", "c": "C"})
    write(project / "messages/fr.json", {"c": "fr-c", "a": "fr-a"})
    assert run().exit_code == 0
    assert list(read(project / "messages/fr.json")) == ["a", "b", "c"]


def test_exclude_copies_without_translating(project):
    (project / "langsync.json").write_text(json.dumps({
        "source": "messages/en.json", "dir": "messages", "exclude": ["meta.*"],
    }))
    write(project / "messages/en.json", {"meta": {"url": "https://x.dev"}, "a": "Hello"})
    write(project / "messages/fr.json", {})
    assert run().exit_code == 0
    assert read(project / "messages/fr.json") == {"meta": {"url": "https://x.dev"}, "a": "fr:Hello"}


def test_pattern_layout_with_namespaces(project):
    (project / "langsync.json").write_text(json.dumps({
        "pattern": "locales/{locale}/{ns}.json", "source_locale": "en",
    }))
    write(project / "locales/en/common.json", {"hi": "Hello"})
    write(project / "locales/en/auth.json", {"login": "Log in"})
    write(project / "locales/fr/common.json", {})
    result = run()
    assert result.exit_code == 0, result.output
    assert read(project / "locales/fr/common.json") == {"hi": "fr:Hello"}
    # A missing namespace file is created for an existing locale.
    assert read(project / "locales/fr/auth.json") == {"login": "fr:Log in"}
    state = read(project / "locales/.langsync-state.json")
    assert set(state["hashes"]) == {"auth:login", "common:hi"}


def test_arb_metadata_is_not_translated_and_locale_is_set(project):
    (project / "langsync.json").write_text(json.dumps({
        "pattern": "l10n/app_{locale}.arb", "source_locale": "en", "locales": ["pt-BR"],
    }))
    write(project / "l10n/app_en.arb", {
        "@@locale": "en", "title": "Hello", "@title": {"description": "Page title"},
    })
    result = run()
    assert result.exit_code == 0, result.output
    data = read(project / "l10n/app_pt-BR.arb")
    assert data["@@locale"] == "pt_BR"
    assert data["title"] == "pt:Hello" or data["title"].endswith(":Hello")
    assert "@title" not in data


def test_json_report_on_stdout(project):
    write(project / "messages/en.json", {"a": "Hello"})
    write(project / "messages/fr.json", {})
    runner = CliRunner()
    result = runner.invoke(cli.main, [*flat_args(), "--check", "--json"], catch_exceptions=False)
    assert result.exit_code == 1
    start = result.stdout.index("{")
    report = json.loads(result.stdout[start:])
    assert report["in_sync"] is False
    assert report["locales"][0]["locale"] == "fr"
    assert report["locales"][0]["missing"] == 1


def test_stats_reports_coverage(project):
    write(project / "messages/en.json", {"a": "Hello", "b": "World"})
    write(project / "messages/fr.json", {"a": "Bonjour"})
    result = run(*flat_args(), "--stats")
    assert result.exit_code == 0, result.output
    assert "50.0%" in result.output


def test_identical_translation_is_not_a_failure(project, monkeypatch):
    class Echo(FakeService):
        def translate_batch(self, texts, delay=0):
            return [None] * len(texts)  # force the per-item path

        def translate_one(self, text, delay=0):
            return text  # "OK" is "OK" in French

    monkeypatch.setattr(cli, "TranslationService", Echo)
    write(project / "messages/en.json", {"ok": "OK"})
    write(project / "messages/fr.json", {})
    result = run(*flat_args())
    assert result.exit_code == 0, result.output
    assert read(project / "messages/fr.json") == {"ok": "OK"}


def test_failed_keys_exit_nonzero_and_lag_only_that_locale(project, monkeypatch):
    class Flaky(FakeService):
        def translate_batch(self, texts, delay=0):
            if self.target_lang == "de":
                raise cli.TranslationError("boom", kind="api")
            return super().translate_batch(texts, delay)

        def translate_one(self, text, delay=0):
            if self.target_lang == "de":
                raise cli.TranslationError("boom", kind="api")
            return super().translate_one(text, delay)

    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    write(project / "messages/en.json", {"a": "Hello"})
    write(project / "messages/fr.json", {"a": "fr:Hello"})
    write(project / "messages/de.json", {"a": "de:Hello"})
    assert run(*flat_args()).exit_code == 0  # bootstrap snapshot

    monkeypatch.setattr(cli, "TranslationService", Flaky)
    write(project / "messages/en.json", {"a": "Hi"})
    result = run(*flat_args())
    assert result.exit_code == 1
    assert read(project / "messages/fr.json") == {"a": "fr:Hi"}

    monkeypatch.setattr(cli, "TranslationService", FakeService)
    assert run(*flat_args()).exit_code == 0
    assert read(project / "messages/de.json") == {"a": "de:Hi"}
    # fr was already done and must not be re-translated.
    assert all(s.target_lang != "fr" for s in FakeService.instances[-1:])


def test_placeholder_mismatch_is_reported(project):
    write(project / "messages/en.json", {"a": "Hello {name}"})
    write(project / "messages/fr.json", {"a": "Bonjour {nom}"})
    result = run(*flat_args(), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "placeholder" in result.output.lower()


def test_invalid_target_json_fails_check(project):
    write(project / "messages/en.json", {"a": "Hello"})
    (project / "messages/fr.json").write_text("{ nope")
    result = run(*flat_args(), "--check")
    assert result.exit_code == 1
