"""Best-effort check for a newer langsync version on GitHub.

Runs on a background thread with a tight timeout, caches the result for a day,
and silently swallows network failures so offline use is unaffected.
"""

import json
import os
import threading
import time
import urllib.request
from urllib.error import URLError

from . import __version__

CACHE_PATH = os.path.expanduser("~/.langsync-update-check.json")
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
PYPROJECT_URL = "https://raw.githubusercontent.com/dracu-lah/langsync-cli/main/pyproject.toml"
FETCH_TIMEOUT = 2.5
ENV_DISABLE = "LANGSYNC_NO_UPDATE_CHECK"


def _parse_version_from_pyproject(text):
    in_project_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project_block = line == "[project]"
            continue
        if not in_project_block:
            continue
        if line.startswith("version"):
            _, _, rhs = line.partition("=")
            v = rhs.strip().strip('"').strip("'")
            if v:
                return v
    return None


def _version_tuple(v):
    parts = []
    for chunk in v.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _read_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_cache(data):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetch_latest_version():
    try:
        req = urllib.request.Request(
            PYPROJECT_URL,
            headers={"User-Agent": f"langsync/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return _parse_version_from_pyproject(text)
    except (URLError, TimeoutError, OSError, ValueError):
        return None


def _compute_update_status():
    if os.environ.get(ENV_DISABLE):
        return None

    cache = _read_cache()
    last_checked = cache.get("checked_at", 0)
    latest = cache.get("latest")

    if not latest or (time.time() - last_checked) > CHECK_INTERVAL_SECONDS:
        fetched = _fetch_latest_version()
        if fetched:
            latest = fetched
            _write_cache({"checked_at": time.time(), "latest": latest})

    if not latest:
        return None

    current_t = _version_tuple(__version__)
    latest_t = _version_tuple(latest)
    if current_t and latest_t and latest_t > current_t:
        return (__version__, latest)
    return None


def start_update_check():
    """Spawn the check on a daemon thread and return a poll function.

    The poll function returns (current, latest) when an update is available,
    or None — either because no update is available, the check is still in
    flight, or it failed.
    """
    holder = {"result": None, "done": False}

    def worker():
        try:
            holder["result"] = _compute_update_status()
        except Exception:
            holder["result"] = None
        finally:
            holder["done"] = True

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def poll(wait_seconds=0.0):
        if not holder["done"] and wait_seconds > 0:
            t.join(timeout=wait_seconds)
        return holder["result"]

    return poll
