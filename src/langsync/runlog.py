"""Always-on, metadata-only run log and the shareable `langsync report`.

Every run appends key=value lines to a rotating local file. Source or translated
text, API keys and user file paths are never written (text only behind
LANGSYNC_LOG_TEXT=1). Nothing here may fail a run: every public function
swallows its own errors.
"""
import heapq
import logging
import os
import platform
import re
import secrets
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

MAX_BYTES = 1_000_000
BACKUPS = 2  # live file + 2 backups = 3 files
SLOW_RUN_SECONDS = 60
KEY_ENV_NAMES = ("DEEPL_API_KEY", "LIBRETRANSLATE_API_KEY", "MICROSOFT_TRANSLATOR_KEY")
HINT_TEXT = "Details: {path}  (run langsync report to share)"

_LOGGER_NAME = "langsync.run"
_lock = threading.Lock()
_state = {"run_id": None, "path": None, "handlers": [], "started": 0.0, "slow": [], "hinted": False}

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|authorization|auth[_-]?key)\b(\s*[=:]\s*)(?:(?:Bearer|DeepL-Auth-Key)\s+)?[^\s,;'\"]+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(Bearer|DeepL-Auth-Key)\s+[A-Za-z0-9._:\-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)([?&](?:key|api_key|auth_key|token)=)[^&\s]+"), r"\1<redacted>"),
]


def enabled():
    return os.environ.get("LANGSYNC_NO_LOG", "") not in ("1", "true", "yes")


def text_logging():
    return os.environ.get("LANGSYNC_LOG_TEXT", "") == "1"


def log_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "langsync", "langsync.log")


def redact(text):
    """Strip secrets, key env values and the home directory from a string."""
    text = str(text)
    for name in KEY_ENV_NAMES:
        val = os.environ.get(name)
        if val and len(val) >= 4:
            text = text.replace(val, "<redacted>")
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    home = os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def _fmt(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (int, type(None))):
        return str(value)
    s = redact(value).replace("\n", " ").replace("\r", " ")
    if len(s) > 200:
        s = s[:200] + "..."
    return f'"{s}"' if (" " in s or "=" in s or not s) else s


def _safe(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _emit(level, event, fields):
    logger = logging.getLogger(_LOGGER_NAME)
    parts = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items())
    logger.log(level, "[%s] %s%s", _state["run_id"] or "-", event, f" {parts}" if parts else "")


@_safe
def start(debug=False, **info):
    """Begin a run: pick a run id, open the rotating file, write the run-start line.
    Returns the run id (or None when logging is off or the file cannot be opened)."""
    stop()
    run_id = secrets.token_hex(3)
    _state.update(run_id=run_id, started=time.monotonic(), slow=[], hinted=False, path=None)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%dT%H:%M:%S")
    if debug or os.environ.get("LANGSYNC_DEBUG") == "1":
        h = logging.StreamHandler(sys.stderr)
        h.setLevel(logging.DEBUG)
        h.setFormatter(fmt)
        logger.addHandler(h)
        _state["handlers"].append(h)
    if enabled():
        try:
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
            fh.setLevel(logging.INFO)
            fh.setFormatter(fmt)
            fh.handleError = lambda record: None  # write errors never fail a run
            logger.addHandler(fh)
            _state["handlers"].append(fh)
            _state["path"] = path
        except OSError:
            pass
    from . import __version__
    _emit(logging.INFO, "run_start", {
        "version": __version__, "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}", **info,
    })
    return run_id


@_safe
def stop():
    logger = logging.getLogger(_LOGGER_NAME)
    for h in _state["handlers"]:
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _state.update(handlers=[], run_id=None)


def run_id():
    return _state["run_id"]


def path():
    """The log file path when this run is writing one, else None."""
    return _state["path"]


@_safe
def info(event, **fields):
    _emit(logging.INFO, event, fields)


@_safe
def debug(event, **fields):
    _emit(logging.DEBUG, event, fields)


@_safe
def warn(event, **fields):
    _emit(logging.WARNING, event, fields)


@_safe
def log_text(label, text):
    """Source/translated text is logged only when LANGSYNC_LOG_TEXT=1."""
    if text_logging():
        _emit(logging.DEBUG, "text", {"label": label, "text": text})


@_safe
def request(provider, texts, chars, packed, latency_ms, status=None, retry=0,
            backoff_wait=None, error=None):
    fields = {"provider": provider, "texts": texts, "chars": chars,
              "packed": packed, "latency_ms": round(latency_ms), "status": status, "retry": retry}
    if backoff_wait:
        fields["backoff_wait"] = backoff_wait
    if error:
        fields["error"] = error
    with _lock:
        heapq.heappush(_state["slow"], (latency_ms, len(_state["slow"]), fields))
        if len(_state["slow"]) > 5:
            heapq.heappop(_state["slow"])
    _emit(logging.WARNING if error else logging.INFO, "request", fields)


@_safe
def governor_wait(kind, wait, reason=None):
    info("governor_wait", kind=kind, wait=wait, reason=reason)


@_safe
def governor_open(reason):
    warn("governor_open", reason=reason, exit=3)


@_safe
def locale_done(locale, translated, failed, copied, orphaned, placeholder_problems, duration):
    info("locale", locale=locale, translated=translated, failed=failed, copied=copied,
         orphaned=orphaned, placeholder_problems=placeholder_problems, duration=round(duration, 2))


@_safe
def placeholder_problem(locale, key_id):
    info("placeholder_problem", locale=locale, key_id=key_id)


@_safe
def finish(exit_code, **totals):
    slow = [f for _, _, f in sorted(_state["slow"], reverse=True)]
    duration = time.monotonic() - _state["started"]
    info("run_end", exit=exit_code, duration=round(duration, 2), **totals)
    for i, f in enumerate(slow, 1):
        info("slowest", rank=i, **{k: v for k, v in f.items() if k != "error"})


@_safe
def exception(exc):
    """Full traceback to the log only."""
    logging.getLogger(_LOGGER_NAME).error(
        "[%s] unhandled_exception %s", _state["run_id"] or "-", redact(f"{type(exc).__name__}: {exc}"),
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def hint():
    """The one-line pointer to the log, or None when no file is being written."""
    p = _state["path"]
    return HINT_TEXT.format(path=redact(p)) if p else None


def slow_hint(elapsed):
    """The hint, once, if the run has gone past the slow threshold."""
    if elapsed >= SLOW_RUN_SECONDS and not _state["hinted"] and _state["path"]:
        _state["hinted"] = True
        return hint()
    return None


# -- report -----------------------------------------------------------------

_RUN_RE = re.compile(r"^\S+ \S+\s+\[(?P<id>[0-9a-f]{6}|-)\] ")


def system_info():
    from . import __version__
    return "\n".join([
        f"langsync: {__version__}",
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"os: {platform.system()} {platform.release()} {platform.machine()}",
        f"log: {redact(log_path())}",
    ])


def _read_lines(base):
    lines = []
    for p in [f"{base}.{i}" for i in range(BACKUPS, 0, -1)] + [base]:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.read().splitlines())
        except OSError:
            continue
    return lines


def _group_runs(lines):
    """Ordered {run_id: [lines]}; continuation lines (tracebacks) stay with the last run."""
    runs, current = {}, None
    for line in lines:
        m = _RUN_RE.match(line)
        if m:
            current = m.group("id")
            runs.setdefault(current, [])
        if current is not None:
            runs[current].append(line)
    return runs


def build_report(run=None, last=1, base=None):
    """Return (text, removed_count, run_ids). The redaction pass is applied again
    to the whole body, and its count is reported to the user."""
    runs = _group_runs(_read_lines(base or log_path()))
    ids = [run] if run else list(runs)[-max(1, last):]
    body = []
    for rid in ids:
        body.extend(runs.get(rid, []))
    raw = "\n".join(body)
    clean = redact(raw)
    removed = sum(1 for a, b in zip(raw.splitlines(), clean.splitlines()) if a != b)
    text = f"# langsync report\n{system_info()}\nruns: {', '.join(ids) or 'none'}\n\n{clean}\n"
    return text, removed, ids
