# Failure record: a large run against a rate-limiting translator (2026-11-01)

Status: open. Written for a maintainer or coding agent who was not present. Nothing in the
tool was changed when this was written. Line numbers refer to the repository at commit
`fd4b6e3` (0.11.0) unless a file is marked "0.10.0" (commit `66d14fa`).

Labels used below: **Verified** means read in the code or reproduced with a command. **Inferred**
means a conclusion from the observed output, not checked.

## 1. What happened

Invocation, from the TMPlayer repository: `langsync -c private/langsync.json`, langsync 0.10.0
installed with pipx. Config: `source` = a 180 KB `en.json`, `max_parallel_locales` 4,
`batch_size` 25, a whitelist of 19 product names. Other keys were left at their defaults.

| Item | Value |
| --- | --- |
| Target locales | 20 (ar, bn, de, es-419, fa, fr, hi, id, it, ja, ko, ml, pl, pt-BR, ru, tr, uk, vi, zh-CN, zh-TW) |
| Missing keys per locale | 134 (2680 translations in all) |
| `--dry-run` | 0.35 s, Missing 2680, Changed 0, Orphans 0 (correct) |
| Real run | 722.68 s. `Translated: 0  Copied: 0  Pruned: 0  Failed: 2680` |
| Exit code | 0 |

Error shapes printed in the summary table:

```
zh-TW  134  api x140  batch of 25 fell back to single-item (Server Error: You made too many requests to the ...)
'browse.add_chats': Server Error: You made too many requests to the ...
```

The snapshot (`.langsync-state.json`) was not advanced for the failed keys, so no work was lost
and a re-run picks the keys up. This is correct behaviour and must be kept.

A later single-locale run (`-l de`) was started and killed by the user before it finished. There
is no data on whether a gentler configuration (one locale at a time, longer delay) succeeds. That
is unknown and must not be assumed either way.

Consequence: the 2680 translations were produced outside langsync to unblock a release. The tool
failed at the scale it exists for.

The failure is **unverified against 0.11.0**. The repository is at 0.11.0 and part of what
happened is already fixed there (sections 3.5 and 3.6). First step for anyone picking this up:
upgrade, rerun the same command (or the fake-API test in section 5) and see what remains.

## 2. How the code behaves today

Verified by reading the code:

- Locales run in a `ThreadPoolExecutor(max_workers=max_parallel_locales)`
  (`src/langsync/cli.py:1321` and `:1334`). Default `MAX_PARALLEL_LOCALES = 3`
  (`src/langsync/config.py:53`).
- Each locale splits its items into batches of `batch_size` and runs them on
  `max_workers_per_locale` threads (`cli.py:380-419`, default 2, `config.py:52`). So the real
  concurrency is up to `max_parallel_locales x max_workers_per_locale`, which was 4 x 2 = 8 in the
  failed run because only the first was overridden. The only shared state is the progress bar and
  the result object. There is no shared limiter, lock or semaphore around the API
  (`grep` for `Semaphore|Lock` finds only `LocaleResult._lock`, `cli.py:92`).
- `_translate_with_fallback` (`cli.py:295-377`) tries `service.translate_batch` up to
  `retry_count` times (default 3, `config.py:56`). Between attempts it sleeps a fixed
  `1 * (attempt + 1)` seconds, or `2 * (attempt + 1)` seconds when the error text contains
  `RATE_LIMIT_HIT` (`cli.py:313-319`). It also doubles the per-request delay up to 2.0 s. These
  are linear, not exponential, per batch only, with no jitter, and nothing is shared between
  threads.
- When all attempts fail it records one issue and then calls `_one_by_one` over every item
  (`cli.py:375-377`). `_one_by_one` calls `service.translate_one` for each of the 25 items with no
  sleep other than `delay` (default 0.2 s, `translator.py:573`) and no check of what the last
  error was.
- `translate_one` wraps provider errors in `TranslationError(str(e), kind=_classify_error(e))`
  (`translator.py:569-570`). `translate_batch` does the same, except that a `rate_limit` error is
  replaced by a bare `Exception("RATE_LIMIT_HIT")`, which discards the provider's message
  (`translator.py:655-660`).
- No code reads a `Retry-After` header or status code. `grep -rn "Retry-After\|retry_after\|429"`
  matches only `_classify_error` string checks and tests. Provider errors reach langsync as
  `deep_translator` exceptions, whose `TooManyRequests` carries only a fixed message
  (`deep_translator/exceptions.py:120`), so the header is not available at that layer in any
  case. A fix that wants it needs either a different client or reading the response from the
  underlying `requests` call.
- The exit code is decided at `cli.py:1675-1680`: `--check` with drift exits 1, and
  `not dry_run and (total_failed or total_io)` exits 1.
- `_truncate_issue` (`cli.py:623-629`) keeps only the first line of a message and cuts it at 160
  characters, and the issue table shows at most two messages per locale, then "... and N more"
  (`cli.py:663-666`).
- Issue "Cause" column counts issues by kind: `api x140` means 140 issues of kind `api`
  (`cli.py:659-660`).

## 3. Defects

### 3.1 No global rate limiting across parallel work

Verified: concurrency multiplies (section 2) and nothing coordinates the threads. A server that
allows N requests per second is hit by every thread independently. The translator's own message
says Google allows about 5 requests per second.

Fix must: put one process-wide limiter (token bucket, requests per second, configurable, with a
sensible default) in front of every API call from `translate_batch` and `translate_one`, shared by
all locales and workers. It should be possible to cap total in-flight requests. The existing
`delay_between_requests` is per thread and sleeps after a call, so it does not do this.

### 3.2 The fallback amplifies load

Verified: after a failed batch of 25, `_one_by_one` immediately issues up to 25 more requests
(`cli.py:330-348`), against the same API that just refused the batch. For 134 keys that is 6
batches (five of 25, one of 9), so up to 134 single calls per locale after the batch attempts.
The "api x140" figure equals 134 failed items plus 6 batch fallback issues, which fits this
exactly (inferred from the arithmetic, consistent with the code).

Fix must: not fall back to per-item calls when the batch failed for a reason that is not about
the batch's content. Per-item fallback is correct only for content problems (a bad item, an
empty or mangled result). For a rate limit, network or server failure it must wait and retry the
batch, or stop (3.4). `_one_by_one` also needs to stop at the first rate-limit error instead of
logging 25 identical failures.

### 3.3 No exponential backoff, no Retry-After

Verified: waits are linear and fixed (section 2), unrelated to what the server asked, and each
thread backs off alone. Total waiting per failed batch is a few seconds, far less than a rate
limit window.

Fix must: exponential backoff with jitter and a cap (for example 1, 2, 4, 8, ... up to 60 s), a
backoff that is shared (a rate-limit seen by one thread pauses the limiter for all), and use of
`Retry-After` (seconds or HTTP date) when the response exposes it, taking the larger of the two
values. Make the cap and the retry count configurable.

### 3.4 No circuit breaker

Verified: nothing counts consecutive failures across locales. The run spent 722 s (inferred: most
of it in sleeps and doomed calls) before reporting 2680 failures.

Fix must: after a configurable number of consecutive rate-limit or network failures (or a failure
ratio over a window), open a breaker: stop starting new batches, let in-flight ones finish, mark
the remaining keys as failed with a clear cause ("run aborted: translator refused N requests in a
row"), write what succeeded, print the rerun command, and exit non-zero. A half-open probe after
a cool-down is optional.

### 3.5 Exit code 0 on total failure

Verified in 0.10.0: the only `sys.exit(1)` after the summary is the `--check` one; a run with
`Failed: 2680` falls off the end of `main` and returns 0 (0.10.0 `cli.py:1153-1156`).
Verified in 0.11.0: this is fixed. `cli.py:1678-1680` exits 1 when `total_failed or total_io`, and
`tests/test_cli.py:226` (`test_failed_keys_exit_nonzero_and_lag_only_that_locale`) covers one
failing locale. Remaining gap: there is no test of every locale failing, and no distinct exit
code. Fix may: use a documented code scheme (for example 1 for a partial sync, 3 for an aborted
run because the translator refused everything) so scripts can tell them apart. Document it in the
README.

### 3.6 Rate limit misclassified as a plain API error (found while tracing the above)

Verified in 0.10.0: `_classify_error` tested `"Too Many Requests" in msg.lower()`, a mixed-case
needle against a lowercased string, which can never match (0.10.0 `translator.py:70-75`). The
actual `deep_translator.TooManyRequests` message ("Server Error: You made too many requests to
the server. According to google, ...") contains no `429` and no `rate` (checked by running it),
so it fell through to kind `api`. That explains the observed `api x140`, the missing
rate-limit hint in the fix list (`cli.py:700` only fires on kind `rate_limit`), the use of the
1-2-3 s sleeps instead of the 2-4-6 s cooldown, and why the full provider message appeared
instead of "rate limit exceeded". Verified in 0.11.0: `_classify_error` now checks the exception
class and the lowercased text (`translator.py:139-168`), and `tests/test_translator.py:566-568`
cover it. So in 0.11.0 the same failure would be classified `rate_limit`, the fix hint would
show, but the behaviour in 3.1 to 3.4 is unchanged and still unfixed.

### 3.7 The summary truncates the real API message

Verified: `_truncate_issue` keeps the first line, at most 160 characters. The deep_translator
message is on one line and is longer than 160 characters, so the important part ("allowed 5
requests per second and 200k per day") is cut. In 0.11.0 the batch-level rate-limit message is
also replaced by the literal "rate limit exceeded" (`cli.py:369-371`) because `translate_batch`
raises `Exception("RATE_LIMIT_HIT")` (`translator.py:655-660`), and the original exception is
only reachable through `__cause__`.

Fix must: keep the full provider message (and status code) on a structured exception instead of
the `RATE_LIMIT_HIT` string sentinel; show the truncated form in the table but write the full
text to `--json` output and to `--verbose`; collapse 134 identical per-item messages into one
line with a count.

### 3.8 Smaller observations

- Worker thread count is `max_parallel_locales x max_workers_per_locale`, but the fix hint for a
  rate limit (`cli.py:700-704`) suggests values that serialise everything without saying so; the
  limiter in 3.1 makes the hint unnecessary.
- Behaviour to keep: failed keys do not advance the snapshot, a failing locale does not stop
  others from being written, and the rerun command is printed (`cli.py:632-644`).

## 4. Test layout today (verified)

- `tests/test_translator.py` mocks `langsync.translator.GoogleTranslator` with `pytest-mock` and
  tests `TranslationService` directly, including `test_translate_batch_rate_limit` (line 93) and
  `_classify_error` (line 566).
- `tests/test_cli.py` replaces `cli.TranslationService` with a `FakeService` (line 12) and drives
  `cli.main` with `click.testing.CliRunner`, asserting `exit_code`. `cli.time.sleep` is
  monkeypatched in the failure test (line 237).
- `tests/test_processor.py`, `test_state.py` and others cover classification and the snapshot.
- CI runs `pytest -q` on Python 3.9, 3.12 and 3.13 (`.github/workflows/ci.yml`).
- No test exercises more than one locale failing at once, concurrency, timing, or call counts.

## 5. The missing heavy test

Name: `tests/test_rate_limit_scale.py`. It drives `cli.main` end to end, like `test_cli.py`, so
the thread pools, fallback, summary and exit code are all in play. Mark it `@pytest.mark.slow`
if the suite adopts markers; with a fake clock (below) it should still run in seconds.

### 5.1 Fixture

- 20 locales (the list in section 1), source catalog with 134 keys (flat, mixed: plain strings,
  a few with placeholders such as `{name}`, a few ICU plurals), config
  `{"max_parallel_locales": 4, "batch_size": 25}` plus defaults. No existing target files, so
  every locale needs all 134 keys.
- Fake clock: monkeypatch `time.sleep` and `time.monotonic` in `langsync.cli` and
  `langsync.translator` (and in the limiter module once it exists) with a thread-safe virtual
  clock that advances by the slept amount. The test then asserts on virtual elapsed time and
  never waits for real.
- Fake translator at the same level as the real one, so classification is also tested: patch
  `langsync.translator.GoogleTranslator` with a class whose `translate_batch(list)` and
  `translate(str)` call a shared `FakeApi`. The fake raises real
  `deep_translator.exceptions.TooManyRequests` (and, for the Retry-After cases, a subclass or
  wrapper carrying `status_code=429` and `headers={"Retry-After": "..."}`). Using the real
  exception class matters: the 0.10.0 bug was invisible to tests that raised
  `Exception("429 Too Many Requests")`.
- `FakeApi` records every call as `(virtual_time, thread_id, kind, n_items)` under a lock.

### 5.2 Fake API behaviours

1. **Token bucket**: capacity C, refill R requests per virtual second. A call with no token fails
   with `TooManyRequests`. Variants: without Retry-After; with `Retry-After: <seconds to next
   token>`; with an HTTP-date `Retry-After`. Suggested values: R = 5, C = 5.
2. **Always failing**: every call raises `TooManyRequests`.
3. **Recovering**: always fails until virtual time T (for example 30 s), then behaves as the
   token bucket (or succeeds).
4. **Hard server error** (500) and **timeout**: to check these are classified `network` or `api`,
   not `rate_limit`, and are not retried as aggressively.
5. **Mixed content failure**: one specific item returns an empty string, to check per-item
   fallback still works for content problems and does not trip the breaker.

### 5.3 Assertions

Bounds below are the target behaviour. A bound that cannot be met by the current design is a
signal to fix the design, not to loosen the test.

Token bucket, no Retry-After, R = 5:
- The run finishes with all 2680 translations written and exit code 0 (the limiter paces itself).
- Observed request rate never exceeds R per virtual second (sliding one-second window over the
  call log, allowing the bucket capacity once).
- Total failed calls (429s) at most 5 percent of total calls, and total calls at most
  2680 / batch_size x 1.5 + 20 x 6 (batches plus slack for retries), not thousands.
- No fallback to single-item calls occurs for a rate-limit failure (zero `translate` calls
  unless a content problem was injected).

Token bucket with Retry-After:
- No call is made earlier than the advertised Retry-After after a 429 (per limiter, since the
  pause is shared).
- Gaps between consecutive retries of one batch grow (exponential) when there is no
  Retry-After, and respect the header when there is one. Assert on the call log, not on sleeps
  alone.

Always failing API:
- The run ends early: total API calls at most a stated budget, for example
  `breaker_threshold x 2` plus calls already in flight (at most `max_parallel_locales x
  max_workers_per_locale`), instead of the roughly 20 x 134 plus batch attempts seen today.
- Virtual elapsed time is bounded (for example under 10 minutes, not 722 s of hammering).
- Exit code is non-zero (and the specific code once defined, section 3.5).
- No locale file is changed, the snapshot file is byte-identical to its value before the run
  (or absent if it was absent), and no temp file is left in the directory.
- The summary names the cause as a rate limit and prints the rerun command.

Recovering API:
- With the breaker threshold high enough to survive 30 virtual seconds of backoff, the run
  completes with exit code 0 and all keys written. With a low threshold, the run aborts, writes
  nothing, and a second run (API healthy) completes the job from the unchanged snapshot.

Partial success:
- Fail only some locales permanently (for example `de` and `ja` always 429, others fine). The
  other 18 locale files are fully written and their snapshot entries advanced; `de` and `ja` are
  untouched and not in the advanced snapshot; exit code non-zero. If the breaker is global, the
  test documents which behaviour is chosen: either the other locales complete before the abort,
  or the abort is only triggered when every recent call fails regardless of locale.

Reporting:
- The per-locale report in `--json` output contains the complete provider message for a rate
  limit (the 160 character table truncation is allowed only in the terminal table), the HTTP
  status when known, and one aggregated line with a count instead of 134 repeated lines.
- The terminal output contains the rate-limit fix hint (guards against the 3.6 regression).

Thread safety:
- Run the token bucket scenario with `max_workers_per_locale` 2 and `max_parallel_locales` 4 and
  assert the call-log rate bound still holds across all 8 threads.

## 6. Smaller unit tests implied

Place them in the existing files, next to the related tests.

`tests/test_translator.py` (beside `_classify_error` at line 566):
- `_classify_error` with the real `TooManyRequests()` default message, a message in different
  cases (`"TOO MANY REQUESTS"`), `"429"`, `"Rate limit"`, a requests `HTTPError` with status
  429, and a 500, which must not be `rate_limit`. Regression guard for 3.6.
- Retry-After parsing (new helper, for example `parse_retry_after(value, now)`): integer
  seconds, fractional seconds, an HTTP date in the future, a date in the past (zero), garbage,
  empty and missing (None), a huge value (capped).
- `translate_batch` raising a structured rate-limit error that keeps the original message and
  status code (replaces the `RATE_LIMIT_HIT` sentinel test at line 93).

`tests/test_cli.py` (beside `test_failed_keys_exit_nonzero_and_lag_only_that_locale`, line 226):
- Exit code mapping: all locales fail gives the "aborted or total failure" code; some fail gives
  the partial code; none gives 0; `--dry-run` gives 0 even with failures simulated; `--check`
  with drift gives 1 and is not confused with a failed run.
- `_truncate_issue`: single line, multi-line, exactly 160 characters, 161, empty.
- `_render_issues_panel`: 134 identical messages collapse to one line with a count.

New `tests/test_ratelimit.py` once a limiter and breaker module exist:
- Token bucket: starts full, refills over virtual time, blocks and wakes waiters, is safe under
  8 threads.
- Backoff schedule: values grow, capped, jitter within bounds (inject the random source).
- Breaker: opens after N consecutive failures, a success resets the count, stays open, optional
  half-open probe.

`tests/test_state.py` / `tests/test_processor.py`:
- Snapshot is not advanced for keys listed in `failed_paths`, including when every key of every
  locale failed (the behaviour worth preserving).

## 7. Order of work

1. Confirm the failure on 0.11.0 using the section 5 token bucket scenario (write the test first;
   expect the call-count and rate assertions to fail, the exit code and classification ones to
   pass).
2. Shared limiter plus exponential backoff with Retry-After (3.1, 3.3).
3. Stop the per-item fallback for non-content errors (3.2).
4. Circuit breaker and exit codes (3.4, 3.5).
5. Message handling and report (3.7).
6. Run the original real-world case once as a manual check, with `-l de` first, and record the
   result in this file, because that data point is still missing.

## Status (2026-11-02)

Fixed in code, not yet verified against the live endpoint (the dev IP was itself
429-blocked when tested). Defaults now favour finishing over speed: one locale at
a time, 1 request/second, up to 100 strings per batch packed into ~3500-character
requests (a reply that does not split back into the same lines is halved and
retried, then packing switches off), shared exponential backoff up to 300 s, 12
retries per batch. Runs that still cannot finish stop with exit 3 and resume from
the snapshot.
