"""Derive a drift-detection baseline from git history.

Used as a *smart bootstrap* on first run when no `.langsync-state.json` snapshot
exists yet: we look for the most recent commit that touched the locale dir
(or, failing that, the source file itself) and use that commit's source JSON
to seed the snapshot hashes. Every step is best-effort — any failure (no git
binary, not a repo, file untracked, malformed JSON, etc.) returns None and the
caller falls back to a normal empty bootstrap.
"""

import json
import os
import shutil
import subprocess

GIT_TIMEOUT_SECONDS = 5


def _run_git(args, cwd=None):
    """Run a git subcommand. Returns stdout on success, None on any error."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def is_git_available():
    return shutil.which("git") is not None


def is_inside_git_repo(cwd=None):
    if not is_git_available():
        return False
    out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return bool(out) and out.strip() == "true"


def _repo_root(cwd=None):
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if not out:
        return None
    root = out.strip()
    return root or None


def _last_commit_touching(repo_path, cwd):
    """Return the most recent commit hash that touched `repo_path`, or None."""
    out = _run_git(["log", "-1", "--format=%H", "--", repo_path], cwd=cwd)
    if not out:
        return None
    commit = out.strip()
    return commit or None


def _show_file_at_commit(commit, repo_path, cwd):
    out = _run_git(["show", f"{commit}:{repo_path}"], cwd=cwd)
    return out


def _to_repo_relative(path, repo_root):
    """Convert a path (absolute or cwd-relative) into a repo-root-relative path
    suitable for `git log -- <path>`. Returns None if `path` is outside the
    repository."""
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, repo_root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    # git wants forward slashes even on platforms where os.sep is '\'.
    return rel.replace(os.sep, "/")


def find_baseline_source(source_path, dir_path, cwd=None):
    """Best-effort recovery of the last-known source JSON from git history.

    Strategy:
      1. Confirm we are inside a git working tree.
      2. Find the most recent commit that touched the locale dir; this is the
         best proxy for "the last time langsync touched things".
      3. If that lookup misses (e.g. dir was never committed), fall back to the
         last commit that touched the source file itself.
      4. Read the source file from that commit and parse it.

    Returns the parsed source dict on success, or None if any step fails.
    """
    cwd = cwd or os.getcwd()
    if not is_inside_git_repo(cwd):
        return None

    repo_root = _repo_root(cwd)
    if not repo_root:
        return None

    dir_rel = _to_repo_relative(dir_path, repo_root)
    source_rel = _to_repo_relative(source_path, repo_root)

    candidates = [p for p in (dir_rel, source_rel) if p]
    if not candidates:
        return None

    for candidate in candidates:
        commit = _last_commit_touching(candidate, cwd=repo_root)
        if not commit:
            continue
        # We always read the SOURCE file from that commit — the dir-touch was
        # just the proxy for "when langsync last ran".
        if not source_rel:
            continue
        content = _show_file_at_commit(commit, source_rel, cwd=repo_root)
        if content is None or not content.strip():
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    return None
