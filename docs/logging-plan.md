# Always-on run log + shareable report

Goal: when a run is slow, fails, or misbehaves, the user sends one file and the developer can see
what happened. No "re-run with --debug" step.

## Decisions (taken by default, change if wrong)

1. **Always on.** Every run appends to a local log. No flag needed.
   Path: `$XDG_STATE_HOME/langsync/langsync.log` (fallback `~/.local/state/langsync/`).
   Rotating: 1 MB x 3 files (stdlib `RotatingFileHandler`). Log write errors never fail a run.
2. **Metadata only.** Never log source or translated text, API keys, or full file paths of user
   content. Log lengths, counts, hashes of keys at most. Redact anything matching key env names.
   Text logging only behind an explicit `LANGSYNC_LOG_TEXT=1`.
3. **One run id** (short random) on every line, so one run can be pulled out of a shared file.
4. **Stdlib `logging`**, no new dependency. `--debug` / `LANGSYNC_DEBUG=1` also mirrors DEBUG to stderr.
   Default file level is INFO plus the per-request lines below.

## What gets logged

- Run start: langsync version, Python, OS, provider, model, locale count, key count, flags used
  (no values of secrets), config file path.
- Per provider request: provider, model, batch size (texts, chars), packed or single, latency ms,
  HTTP status, retry number, backoff wait, tokens if the API returns them.
- Governor: when it slows down, the wait it imposed, why, and when it opens (exit 3) with the reason.
- Per locale: keys translated / failed / copied / orphaned, placeholder problems (count + key id), duration.
- Run end: exit code, total duration, totals, slowest 5 requests.
- Unhandled exceptions: full traceback to the log, short message to the user with the log path.

## User-facing

- `langsync report` : prints the log path, and writes `langsync-report.txt` (last run only, by run id
  or `--last N`) containing the system info block + that run's lines. Redaction pass applied and
  the command says what it removed. User reviews, then attaches it to an issue.
- Failure and slow-run messages end with: `Details: <log path>  (run langsync report to share)`.
- A run slower than a threshold (e.g. 60 s with no progress) prints the same hint once.
- README gets a "Reporting a problem" section; issue template asks for the report file.

## Files likely touched

- new `src/langsync/runlog.py` (setup, run id, redaction, report builder)
- `src/langsync/cli.py` (call setup at start of `main`, `report` subcommand, hint on errors/exit 1/3)
- translator / governor modules (log lines at request and wait points)
- `tests/test_runlog.py` (redaction, rotation, never-logs-text, log failure does not break run)
- `README.md`, `.github/ISSUE_TEMPLATE/bug.md`

## Open questions for the user (defaults above if no answer)

- Is writing a log by default OK for a CLI people run in CI? Default: yes, and
  `LANGSYNC_NO_LOG=1` turns it off.
- Should `report` ever upload anywhere? Default: no, file only, user attaches it.

## Progress / handoff

- Done (uncommitted, 386 tests pass): `runlog.py` + `tests/test_runlog.py`, `tests/conftest.py` (isolates
  XDG_STATE_HOME), wiring in `translator._gated` (per request), `ratelimit` (wait/open), `cli.py`
  (`--debug`, run start/end, per-locale, hint on `_die`/exit 1/3, unhandled exception, `report` command
  via `cli.entry`, which pyproject now points at), README "Reporting a problem", bug_report.yml field.
- Slow-run hint wired (uncommitted, 387 tests pass): `cli._slow_tick` called every 1 s from the locale
  `wait()` loop in `main`; test in `tests/test_runlog.py`. Threshold is 60 s since run start.
- Not done: tokens field (providers do not return them); `model` is always empty (service has no model attr).
- Gotcha: `console.file` is switched to stderr under `--json`; keep log output off stdout.
- Gotcha: CliRunner mixes stderr into `result.stdout`; tests must parse JSON with `raw_decode`.
