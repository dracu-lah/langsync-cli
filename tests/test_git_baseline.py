"""Smoke tests for the git-based drift baseline.

Most tests skip if git isn't available so the suite stays portable.
"""

import json
import os
import shutil
import subprocess

import pytest

from langsync.git_baseline import (
    find_baseline_source,
    is_git_available,
    is_inside_git_repo,
)


requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not on PATH"
)


def _git(args, cwd):
    """Run git, asserting success. Used to build fixture repos."""
    subprocess.run(
        ["git"] + args,
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def _init_repo(path):
    _git(["init", "-q", "-b", "main"], cwd=str(path))


def test_is_git_available_is_boolean():
    assert isinstance(is_git_available(), bool)


def test_is_inside_git_repo_false_outside(tmp_path):
    # tmp_path is not a git repo.
    assert is_inside_git_repo(cwd=str(tmp_path)) is False


def test_find_baseline_source_returns_none_outside_repo(tmp_path):
    src = tmp_path / "messages" / "en.json"
    src.parent.mkdir(parents=True)
    src.write_text("{}", encoding="utf-8")
    assert find_baseline_source(str(src), str(src.parent), cwd=str(tmp_path)) is None


@requires_git
def test_find_baseline_recovers_prior_source(tmp_path):
    _init_repo(tmp_path)
    msgs = tmp_path / "messages"
    msgs.mkdir()
    source = msgs / "en.json"
    target = msgs / "es.json"

    # Commit a baseline: source + a target.
    source.write_text(json.dumps({"hello": "Hello", "bye": "Goodbye"}), encoding="utf-8")
    target.write_text(json.dumps({"hello": "Hola", "bye": "Adios"}), encoding="utf-8")
    _git(["add", "."], cwd=str(tmp_path))
    _git(["commit", "-q", "-m", "initial sync"], cwd=str(tmp_path))

    # Edit the source in the working tree without committing.
    source.write_text(json.dumps({"hello": "Hi", "bye": "Bye"}), encoding="utf-8")

    baseline = find_baseline_source(str(source), str(msgs), cwd=str(tmp_path))
    assert baseline == {"hello": "Hello", "bye": "Goodbye"}


@requires_git
def test_find_baseline_handles_untracked_source(tmp_path):
    # Repo exists but the source file is brand-new and uncommitted.
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    _git(["add", "README.md"], cwd=str(tmp_path))
    _git(["commit", "-q", "-m", "seed"], cwd=str(tmp_path))

    msgs = tmp_path / "messages"
    msgs.mkdir()
    source = msgs / "en.json"
    source.write_text("{}", encoding="utf-8")

    assert find_baseline_source(str(source), str(msgs), cwd=str(tmp_path)) is None


@requires_git
def test_find_baseline_handles_malformed_previous_source(tmp_path):
    _init_repo(tmp_path)
    msgs = tmp_path / "messages"
    msgs.mkdir()
    source = msgs / "en.json"
    # Commit a non-JSON blob at the source path (edge case: previous commit
    # didn't have langsync set up yet and the file was a placeholder).
    source.write_text("placeholder, not json", encoding="utf-8")
    _git(["add", "."], cwd=str(tmp_path))
    _git(["commit", "-q", "-m", "pre-langsync"], cwd=str(tmp_path))

    # Now make source valid JSON in the working tree.
    source.write_text(json.dumps({"hello": "Hi"}), encoding="utf-8")

    # Baseline lookup must NOT raise — it returns None for a clean fallback.
    assert find_baseline_source(str(source), str(msgs), cwd=str(tmp_path)) is None
