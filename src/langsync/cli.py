import os
import sys
import copy
import json
import click
import time
import signal
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.panel import Panel
from rich.json import JSON

from . import icu
from . import fileio
from . import validate
from .translator import TranslationService, TranslationError, resolve_language
from .processor import LocaleProcessor
from .config import (
    load_config, find_local_config, GLOBAL_CONFIG_PATH, get_starter_config, save_config,
)
from .layout import FlatLayout, PatternLayout, detect_project, looks_like_locale
from .state import (
    STATE_FILENAME,
    Snapshot,
    advance_snapshot,
    compute_source_hashes,
    load_snapshot,
    path_to_key,
    save_snapshot,
)
from .git_baseline import find_baseline_source, is_inside_git_repo
from .update_check import start_update_check
from . import __version__

console = Console()

PROVIDERS = ("google", "deepl", "libre", "microsoft", "mymemory")

# Where each provider's API key is looked up when the config names none.
PROVIDER_KEY_ENV = {
    "deepl": "DEEPL_API_KEY",
    "libre": "LIBRETRANSLATE_API_KEY",
    "microsoft": "MICROSOFT_TRANSLATOR_KEY",
}


def handle_sigint(signum, frame):
    """Gracefully handle Ctrl+C. Files are written atomically, so an interrupted
    run never leaves a half-written locale behind."""
    console.print("\n[bold red]✖ Interrupted by user. Exiting...[/bold red]")
    sys.exit(130)


class LocaleResult:
    """Outcome of processing a single locale. Holds counts and human-readable issues.

    Batches of one locale run on several threads, so every mutation that can
    race goes through a method holding `_lock`."""

    __slots__ = (
        "locale", "translated", "copied", "failed", "pruned",
        "missing_count", "missing_translatable", "changed_count", "orphan_count",
        "unchanged_count", "invalid_count", "identical_count", "shape_count",
        "total_strings", "created", "issues", "failed_paths", "failed_prefixes",
        "suspects", "_lock",
    )

    def __init__(self, locale):
        self.locale = locale
        self.translated = 0
        self.copied = 0
        self.failed = 0
        self.pruned = 0
        self.missing_count = 0
        self.missing_translatable = 0
        self.changed_count = 0
        self.orphan_count = 0
        self.unchanged_count = 0
        self.invalid_count = 0  # target strings with broken ICU (re-translated)
        self.identical_count = 0  # target equals source, probably never translated
        self.shape_count = 0  # string in one file, object/array in the other
        self.total_strings = 0  # translatable source strings, for coverage
        self.created = []  # target files this run created
        self.issues = []  # list of (kind, message)
        self.failed_paths = set()  # snapshot keys the run could not sync
        self.failed_prefixes = set()  # namespaces whose target file could not be read
        self.suspects = []  # (snapshot key, problem) for existing translations
        self._lock = threading.Lock()

    def add_issue(self, kind, message):
        with self._lock:
            self.issues.append((kind, message))

    def mark_failed(self, key):
        with self._lock:
            self.failed += 1
            self.failed_paths.add(key)

    def add_translated(self, n=1):
        with self._lock:
            self.translated += n

    @property
    def status(self):
        if self.failed_prefixes and not (self.translated or self.copied or self.pruned):
            return "failed"
        if self.failed and self.translated == 0 and self.copied == 0 and self.pruned == 0:
            return "failed"
        if self.failed or self.failed_prefixes:
            return "partial"
        if self.translated or self.copied or self.pruned or self.created:
            return "done"
        return "uptodate"

    @property
    def coverage(self):
        if not self.total_strings:
            return 100.0
        done = self.total_strings - self.missing_translatable - self.invalid_count
        return max(0.0, 100.0 * done / self.total_strings)

    def to_dict(self):
        return {
            "locale": self.locale,
            "status": self.status,
            "missing": self.missing_count,
            "changed": self.changed_count,
            "broken_icu": self.invalid_count,
            "orphans": self.orphan_count,
            "unchanged": self.unchanged_count,
            "same_as_source": self.identical_count,
            "translated": self.translated,
            "copied": self.copied,
            "pruned": self.pruned,
            "failed": self.failed,
            "coverage": round(self.coverage, 1),
            "created": self.created,
            "failed_keys": sorted(self.failed_paths),
            "placeholder_problems": [{"key": k, "problem": p} for k, p in self.suspects],
            "issues": [{"kind": k, "message": m} for k, m in self.issues],
        }


class RunOptions:
    """Flags and resolved settings shared by every locale worker."""

    def __init__(self, **kwargs):
        self.rewrite = False
        self.prune = False
        self.update_changed = False
        self.dry_run = False
        self.verbose = False
        self.source_lang = "auto"
        self.provider = "google"
        self.api_key = None
        self.key_order = "keep"
        self.__dict__.update(kwargs)


def _format_path(path):
    return path_to_key(path)


def _fix_block(fixes):
    """Render fix hints. Each entry is either a plain string (advice) or a
    (what, command) pair rendered with the command on its own indented line so
    it can be copy-pasted straight into a shell."""
    lines = []
    for fix in fixes:
        if not fix:
            continue
        if isinstance(fix, (tuple, list)):
            what, command = fix
            lines.append(f"[dim]→[/dim] {what}\n  [bold cyan]{command}[/bold cyan]")
        else:
            lines.append(f"[dim]→[/dim] {fix}")
    return "\n".join(lines)


def _print_fixes(*fixes, title="How to fix"):
    """Print a panel of actionable next steps. No-op when there is nothing to say."""
    block = _fix_block(fixes)
    if not block:
        return
    console.print(Panel(
        block,
        title=f"[bold cyan]💡 {title}[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))


def _die(message, *fixes, detail=None, code=1):
    """Print an error, the way to fix it, and exit."""
    console.print(f"[bold red]✖ {message}[/bold red]")
    if detail:
        console.print(f"[dim]{detail}[/dim]")
    _print_fixes(*fixes)
    sys.exit(code)


def _config_label(loaded_path):
    """Name the config in fix hints — relative when it lives under the cwd, so the
    hint stays readable instead of wrapping an absolute path across three lines."""
    if not loaded_path:
        return "langsync.json"
    try:
        relative = os.path.relpath(loaded_path)
    except ValueError:  # different drive on Windows
        return loaded_path
    return relative if not relative.startswith("..") else loaded_path


def _child(container, key):
    if isinstance(key, int) and not isinstance(key, bool):
        return container[key] if isinstance(container, list) and 0 <= key < len(container) else None
    return container.get(key) if isinstance(container, dict) else None


def _get_by_path(data, path):
    current = data
    for key in path:
        current = _child(current, key)
        if current is None:
            return None
    return current


def _count_strings(node, skip=None, path=()):
    """Number of translatable (non-blank string) leaves in a source tree."""
    if isinstance(node, dict):
        return sum(
            _count_strings(v, skip, path + (k,)) for k, v in node.items()
            if not (skip and skip(list(path + (k,))))
        )
    if isinstance(node, list):
        return sum(_count_strings(v, skip, path + (i,)) for i, v in enumerate(node))
    return 1 if isinstance(node, str) and node.strip() else 0


def _is_arb_metadata(path):
    """ARB files carry "@key" description entries and "@@locale" next to the
    messages. They are metadata for tooling, never text to translate."""
    return bool(path) and isinstance(path[0], str) and path[0].startswith("@")


def _scoped(hashes, prefix):
    """The slice of a snapshot that belongs to one namespace, prefix removed."""
    if not prefix:
        return dict(hashes)
    return {k[len(prefix):]: v for k, v in hashes.items() if k.startswith(prefix)}


def _prefixed_hashes(units):
    out = {}
    for unit in units:
        for key, h in compute_source_hashes(unit.data).items():
            out[unit.prefix + key] = h
    return out


def _serialized(data):
    # Key order matters for diffs, and dict equality ignores it.
    return json.dumps(data, ensure_ascii=False)


def _glossary_for(config, locale, lang_code):
    """Merge glossary entries keyed by base language, provider code and full
    locale — most specific wins (fr < fr-CA)."""
    glossary = config.get("glossary") or {}
    if not isinstance(glossary, dict):
        return None
    base = locale.replace("_", "-").split("-")[0]
    merged = {}
    for key in (base, lang_code, locale, locale.replace("_", "-")):
        entry = glossary.get(key)
        if isinstance(entry, dict):
            merged.update(entry)
    return merged or None


def _api_key(config, provider):
    if config.get("api_key"):
        return config["api_key"]
    env_name = config.get("api_key_env") or PROVIDER_KEY_ENV.get(provider)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return os.environ.get("LANGSYNC_API_KEY")


def _translate_with_fallback(service, batch, retry_count, delay, result, progress, locale_task_id, verbose):
    """Translate a batch. If batch fails, fall back to per-item translation.
    Mutates `result` (counts + issues). Returns list of (ref, translated_value)
    for successfully translated items. `ref` is (unit index, path, snapshot key).
    """
    refs = [item[0] for item in batch]
    values = [item[1] for item in batch]

    translated_values = None
    last_batch_error = None

    current_delay = delay
    for attempt in range(retry_count):
        try:
            translated_values = service.translate_batch(values, current_delay)
            if translated_values and len(translated_values) == len(values):
                break
        except Exception as e:
            last_batch_error = e
            if "RATE_LIMIT_HIT" in str(e):
                cooldown = 2 * (attempt + 1)
                time.sleep(cooldown)
                current_delay = min(max(current_delay, 0.1) * 2, 2.0)
            else:
                time.sleep(1 * (attempt + 1))
            translated_values = None

    succeeded = []

    def _log(ref, value):
        if verbose:
            progress.console.print(
                rf"[dim]\[{result.locale}][/dim] Translated [blue]{ref[2]}[/blue] -> [italic]{value}[/italic]"
            )

    def _one_by_one(pairs):
        for ref, src_val in pairs:
            try:
                trans_val = service.translate_one(src_val, delay=current_delay)
                # A translation identical to the source is valid ("OK", "Email",
                # brand names) — only an empty answer is a failure.
                if isinstance(trans_val, str) and (trans_val.strip() or not src_val.strip()):
                    succeeded.append((ref, trans_val))
                    _log(ref, trans_val)
                else:
                    result.mark_failed(ref[2])
                    result.add_issue("empty", f"empty response for '{ref[2]}'")
            except TranslationError as e:
                result.mark_failed(ref[2])
                result.add_issue(e.kind, f"'{ref[2]}': {e}")
            except Exception as e:
                result.mark_failed(ref[2])
                result.add_issue("rate_limit" if "RATE_LIMIT_HIT" in str(e) else "unknown", f"'{ref[2]}': {e}")
            finally:
                progress.update(locale_task_id, advance=1)

    if translated_values and len(translated_values) == len(values):
        per_item_failures = []
        for ref, src_val, trans_val in zip(refs, values, translated_values):
            if trans_val is None or trans_val == "":
                per_item_failures.append((ref, src_val))
            else:
                succeeded.append((ref, trans_val))
                progress.update(locale_task_id, advance=1)
                _log(ref, trans_val)
        _one_by_one(per_item_failures)
        return succeeded

    # Batch failed entirely after retries — fall back to per-item.
    err_kind = "unknown"
    err_msg = "batch translation failed"
    if isinstance(last_batch_error, TranslationError):
        err_kind = last_batch_error.kind
        err_msg = str(last_batch_error)
    elif last_batch_error and "RATE_LIMIT_HIT" in str(last_batch_error):
        err_kind = "rate_limit"
        err_msg = "rate limit exceeded"
    elif last_batch_error:
        err_msg = str(last_batch_error)

    result.add_issue(err_kind, f"batch of {len(values)} fell back to single-item ({err_msg})")
    _one_by_one(list(zip(refs, values)))
    return succeeded


def _translate_items(items, make_service, config, result, progress, locale_task_id, verbose):
    """Translate every (ref, value) item of one locale. Batches run on up to
    `max_workers_per_locale` threads; each thread gets its own translator
    because deep-translator clients keep per-request state on the instance."""
    batch_size = max(1, int(config.get("batch_size") or 25))
    delay = config.get("delay_between_requests", 0.2)
    retry_count = max(1, int(config.get("retry_count") or 3))
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    workers = max(1, min(int(config.get("max_workers_per_locale") or 1), len(batches)))

    local = threading.local()

    def service():
        svc = getattr(local, "svc", None)
        if svc is None:
            svc = make_service()
            local.svc = svc
        return svc

    def run(batch):
        return _translate_with_fallback(
            service(), batch, retry_count, delay, result, progress, locale_task_id, verbose,
        )

    succeeded = []
    if workers == 1:
        for batch in batches:
            succeeded.extend(run(batch))
        return succeeded

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, batch): batch for batch in batches}
        for future in as_completed(futures):
            try:
                succeeded.extend(future.result())
            except Exception as e:
                for ref, _ in futures[future]:
                    result.mark_failed(ref[2])
                result.add_issue("crash", f"translation worker crashed: {e}")
    return succeeded


def process_locale(locale, units, layout, progress, main_task_id, config, *, snapshot, opts):
    result = LocaleResult(locale)
    try:
        _process_locale(result, units, layout, progress, config, snapshot, opts)
    finally:
        progress.update(main_task_id, advance=1)
    return result


def _process_locale(result, units, layout, progress, config, snapshot, opts):
    locale = result.locale
    locale_hashes = snapshot.for_locale(locale)
    exclude = config.get("exclude") or None
    multi = len(units) > 1 or units[0].ns is not None

    entries = []
    items = []  # ((entry index, path, snapshot key), source value)

    for unit in units:
        target_file = layout.target_path(locale, unit.ns)
        existed = os.path.exists(target_file)
        try:
            target_data, style = fileio.load(target_file)
        except json.JSONDecodeError as e:
            result.add_issue("io", f"{target_file} is not valid JSON ({e.msg}, line {e.lineno}); skipped it")
            result.failed_prefixes.add(unit.prefix)
            continue
        except fileio.FormatError as e:
            result.add_issue("io", f"{target_file}: {e}; skipped it")
            result.failed_prefixes.add(unit.prefix)
            continue
        except OSError as e:
            result.add_issue("io", f"failed to read {target_file}: {e}")
            result.failed_prefixes.add(unit.prefix)
            continue

        if not isinstance(target_data, dict):
            result.add_issue(
                "io",
                f"{target_file} must contain an object at the top level "
                f"(found {type(target_data).__name__}); skipped it",
            )
            result.failed_prefixes.add(unit.prefix)
            continue

        if not existed and unit.style is not None:
            # A brand-new locale file is formatted like its source.
            style = copy.copy(unit.style)
            style.bom = False
            style.duplicates = []
        if getattr(style, "duplicates", None):
            shown = ", ".join(style.duplicates[:3]) + (" …" if len(style.duplicates) > 3 else "")
            result.add_issue("duplicate", f"{target_file} has duplicate keys ({shown}); the last one wins")

        skip = _is_arb_metadata if unit.is_arb else None
        classification = LocaleProcessor(unit.data).classify_keys(
            target_data,
            snapshot_hashes=_scoped(locale_hashes, unit.prefix),
            force_rewrite=opts.rewrite,
            exclude=exclude,
            skip_keys=skip,
        )

        missing_translatable = classification["missing_translatable"]
        missing_passthrough = classification["missing_passthrough"]
        changed_translatable = classification["changed_translatable"]
        changed_passthrough = classification["changed_passthrough"]
        invalid_targets = classification["invalid_target"]
        orphan_paths = classification["orphans"]

        result.missing_count += len(missing_translatable) + len(missing_passthrough)
        result.missing_translatable += len(missing_translatable)
        result.changed_count += len(changed_translatable) + len(changed_passthrough)
        result.invalid_count += len(invalid_targets)
        result.orphan_count += len(orphan_paths)
        result.unchanged_count += len(classification["unchanged"])
        result.identical_count += len(classification.get("identical", []))
        result.shape_count += len(classification.get("shape_mismatch", []))
        result.total_strings += _count_strings(unit.data, skip)

        # `--update-changed` narrows the run to drift-only; rewrite still wins.
        # Broken ICU targets are always healed — they render as nothing as-is.
        if opts.update_changed and not opts.rewrite:
            missing_translatable = []
            missing_passthrough = []

        translatable = missing_translatable + changed_translatable + invalid_targets
        passthrough = missing_passthrough + changed_passthrough
        result.copied += len(passthrough)

        # Existing translations whose placeholders or tags no longer match the
        # source render wrong at runtime. Report them; never overwrite by guess.
        for path in classification["unchanged"]:
            src_val = _get_by_path(unit.data, path)
            tgt_val = _get_by_path(target_data, path)
            if isinstance(src_val, str) and isinstance(tgt_val, str) and src_val != tgt_val:
                for problem in validate.check_translation(src_val, tgt_val):
                    result.suspects.append((unit.prefix + path_to_key(path), problem))

        label = (lambda p: unit.prefix + _format_path(p)) if multi else _format_path
        if opts.dry_run and opts.verbose:
            for path, val in missing_translatable:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [yellow]Missing:[/yellow] [blue]{label(path)}[/blue] -> [italic]{val}[/italic]"
                )
            for path, val in changed_translatable:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [red]Changed:[/red] [blue]{label(path)}[/blue] -> [italic]{val}[/italic]"
                )
            for path, _ in invalid_targets:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [bright_red]Broken ICU:[/bright_red] [blue]{label(path)}[/blue] (will re-translate)"
                )
            for path, _ in passthrough:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [magenta]Copy as-is:[/magenta] [blue]{label(path)}[/blue]"
                )
            for path in orphan_paths:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [bright_black]Orphan:[/bright_black] [blue]{label(path)}[/blue]"
                )
            for path in classification.get("identical", []):
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [dim]Same as source:[/dim] [blue]{label(path)}[/blue]"
                )

        entry_index = len(entries)
        entries.append({
            "unit": unit,
            "file": target_file,
            "existed": existed,
            "data": target_data,
            "style": style,
            "before": _serialized(target_data),
            "passthrough": passthrough,
            "orphans": orphan_paths,
        })
        for path, val in translatable:
            items.append(((entry_index, path, unit.prefix + path_to_key(path)), val))

    if opts.dry_run:
        return

    for entry in entries:
        for path, val in entry["passthrough"]:
            LocaleProcessor.set_value_by_path(entry["data"], path, val)
        # Optional orphan removal. Remove deepest/highest paths first so deleting a
        # list element never shifts a sibling index we still have to remove.
        if opts.prune:
            for path in sorted(entry["orphans"], key=lambda p: [(isinstance(k, str), k) for k in p], reverse=True):
                LocaleProcessor.remove_by_path(entry["data"], path)
                result.pruned += 1

    if items:
        provider = opts.provider
        lang_code = resolve_language(locale, provider, config.get("language_map"))

        def make_service():
            return TranslationService(
                source_lang=opts.source_lang,
                target_lang=lang_code,
                whitelist=config.get("whitelist"),
                provider=provider,
                api_key=opts.api_key,
                provider_url=config.get("provider_url"),
                glossary=_glossary_for(config, locale, lang_code),
            )

        try:
            make_service()  # fail fast on an unsupported language or a missing key
        except Exception as e:
            for ref, _ in items:
                result.mark_failed(ref[2])
            result.add_issue("init", f"could not init the {provider} translator for '{lang_code}': {e}")
        else:
            locale_task_id = progress.add_task(f"[cyan]{locale}", total=len(items))
            succeeded = _translate_items(
                items, make_service, config, result, progress, locale_task_id, opts.verbose,
            )
            for (entry_index, path, _key), trans_val in succeeded:
                LocaleProcessor.set_value_by_path(entries[entry_index]["data"], path, trans_val)
            result.add_translated(len(succeeded))
            progress.remove_task(locale_task_id)

    for entry in entries:
        data = entry["data"]
        unit = entry["unit"]
        if unit.is_arb:
            LocaleProcessor.set_arb_locale(data, locale)
        data = LocaleProcessor.reorder(unit.data, data, opts.key_order)
        if entry["existed"] and _serialized(data) == entry["before"]:
            continue  # nothing changed — leave the file (and its mtime) alone
        try:
            fileio.save(entry["file"], data, entry["style"])
            if not entry["existed"]:
                result.created.append(entry["file"])
        except Exception as e:
            result.add_issue("io", f"failed to write {entry['file']}: {e}")
            result.failed_prefixes.add(unit.prefix)


def _truncate_issue(message, limit=160):
    """Keep the issues table readable — some upstream errors (e.g. deep-translator's
    unsupported-language error) embed the entire supported-language table."""
    first_line = str(message).split("\n", 1)[0]
    if len(first_line) <= limit:
        return first_line
    return first_line[:limit].rstrip() + " …"


def _retry_command(results):
    """The command that retries the failed keys. Locales whose translator could not
    even be constructed are excluded — re-running them changes nothing."""
    unfixable = {
        r.locale for r in results
        if any(kind == "init" for kind, _ in r.issues)
    }
    retryable = sorted({r.locale for r in results if r.failed and r.locale not in unfixable})
    if not retryable:
        return None
    if len(retryable) == len([r for r in results if r.locale not in unfixable]):
        return "langsync"
    return f"langsync --locales {','.join(retryable)}"


def _render_issues_panel(results):
    """Build a clean panel summarizing issues per locale, or return None if all clean."""
    locales_with_issues = [r for r in results if r.issues or r.failed]
    if not locales_with_issues:
        return None

    table = Table(box=None, header_style="bold underline white", expand=True)
    table.add_column("Locale", style="cyan", no_wrap=True)
    table.add_column("Failed", style="red", justify="right")
    table.add_column("Cause", style="yellow", no_wrap=True)
    table.add_column("Details", style="white", overflow="fold")

    for r in sorted(locales_with_issues, key=lambda x: x.locale):
        kinds = Counter(kind for kind, _ in r.issues)
        cause_summary = ", ".join(f"{k} x{v}" for k, v in kinds.most_common())

        sample_messages = [_truncate_issue(msg) for _, msg in r.issues[:2]]
        if len(r.issues) > 2:
            sample_messages.append(f"... and {len(r.issues) - 2} more")
        details = "\n".join(sample_messages) if sample_messages else "—"

        table.add_row(
            r.locale,
            str(r.failed) if r.failed else "—",
            cause_summary or "—",
            details,
        )

    return Panel.fit(
        table,
        title="[bold yellow]⚠ Issues encountered[/bold yellow]",
        border_style="yellow",
    )


def _issue_fixes(results, config_label, layout, provider):
    """Map the issue kinds a run produced onto concrete commands that fix them."""
    locales_with_issues = [r for r in results if r.issues or r.failed]
    if not locales_with_issues:
        return []

    all_kinds = Counter(k for r in locales_with_issues for k, _ in r.issues)
    locales_by_kind = {}
    for r in locales_with_issues:
        for kind, _ in r.issues:
            locales_by_kind.setdefault(kind, set()).add(r.locale)
    retry_cmd = _retry_command(results) or "langsync"

    fixes = []
    # Kinds that a plain re-run fixes are folded into a single retry line at the
    # end — repeating the same command per kind is noise.
    retry_reasons = []

    if all_kinds.get("rate_limit"):
        fixes.append((
            f"The translator rate-limited the run. Slow it down in [bold]{config_label}[/bold]:",
            '"delay_between_requests": 0.5, "max_parallel_locales": 1, "max_workers_per_locale": 1',
        ))
        retry_reasons.append("rate limits")
    if all_kinds.get("network"):
        fixes.append("Network errors — check your connection before retrying.")
        retry_reasons.append("network errors")
    if all_kinds.get("api"):
        fixes.append(("The translator API rejected some items. See exactly which ones:", f"{retry_cmd} -v"))
    if all_kinds.get("empty"):
        retry_reasons.append("empty responses")
    if all_kinds.get("placeholder"):
        fixes.append(
            "The translator dropped or mangled a placeholder, so that translation was rejected — "
            "nothing broken was written."
        )
        retry_reasons.append("mangled placeholders")
    if all_kinds.get("icu"):
        fixes.append(
            "A translation would have broken ICU syntax and was rejected — the old value is intact, "
            "nothing was corrupted."
        )
        retry_reasons.append("rejected ICU translations")
    if all_kinds.get("duplicate"):
        fixes.append(
            "A locale file has the same key twice; JSON keeps only the last one. Delete the "
            "duplicate by hand — langsync rewrites the file without it on the next change."
        )
    if all_kinds.get("io"):
        bad_locale = sorted(locales_by_kind.get("io", {"<locale>"}))[0]
        bad_file = layout.target_path(bad_locale, None) if layout.kind == "flat" else layout.describe()
        fixes.append((
            "A locale file could not be read or written. Check permissions, then validate it:",
            f"ls -l {os.path.dirname(bad_file) or '.'} && python -m json.tool {bad_file}",
        ))
    if all_kinds.get("init"):
        bad = locales_by_kind.get("init", set())
        healthy = [r.locale for r in sorted(results, key=lambda x: x.locale) if r.locale not in bad]
        details = " ".join(m for r in results for k, m in r.issues if k == "init").lower()
        if "api key" in details or "api_key" in details:
            env_name = PROVIDER_KEY_ENV.get(provider, "LANGSYNC_API_KEY")
            fixes.append((
                f"The {provider} provider needs an API key. Export it, or set [bold]\"api_key_env\"[/bold] "
                f"in [bold]{config_label}[/bold]:",
                f"export {env_name}=<your key>",
            ))
        else:
            fixes.append((
                f"The translator does not support {', '.join(sorted(bad))}. Map it to a supported code "
                f"in [bold]{config_label}[/bold] ([bold]\"language_map\": {{\"{sorted(bad)[0]}\": \"<code>\"}}[/bold]), "
                "or skip it for now:",
                f"langsync --locales {','.join(healthy) if healthy else '<locales>'}",
            ))
    if all_kinds.get("crash"):
        fixes.append(("A worker crashed. Re-run with full output and report it if it persists:", "langsync -v"))
    if all_kinds.get("unknown"):
        retry_reasons.append("unexpected errors")

    if retry_reasons and _retry_command(results):
        fixes.append((
            f"Retry the failed keys ({', '.join(retry_reasons)}) — the snapshot was not advanced for "
            "them, so nothing was lost:",
            retry_cmd,
        ))
    return fixes


def _print_update_banner(update_info):
    if not update_info:
        return
    current, latest = update_info
    body = (
        f"[bold yellow]⬆ A newer version of langsync is available: "
        f"[white]{current}[/white] → [green]{latest}[/green][/bold yellow]\n"
        "[dim]Upgrade:[/dim] [cyan]pipx install --force git+https://github.com/dracu-lah/langsync-cli[/cyan]\n"
        "[dim]Disable this check by setting LANGSYNC_NO_UPDATE_CHECK=1[/dim]"
    )
    console.print(Panel(body, border_style="yellow", expand=False))


def _split_locales(raw):
    """Trim, drop empties, and dedupe while preserving the order the user typed."""
    seen = set()
    out = []
    for part in (raw or "").split(","):
        loc = part.strip()
        if loc and loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out


def _interactive(yes, json_output):
    if yes or json_output:
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ensure_config(config, source, dir, interactive):
    """Make sure there is something to run with. Prompts only on a terminal;
    explicit --source/--dir never need a config file at all."""
    if config or (source and dir) or find_local_config():
        return

    if os.path.exists(GLOBAL_CONFIG_PATH):
        if not interactive:
            return  # use the global config silently
        try:
            with open(GLOBAL_CONFIG_PATH, 'r', encoding='utf-8') as f:
                global_content = f.read()
            console.print(Panel(
                JSON(global_content),
                title=f"[bold cyan]Global Configuration Found: {GLOBAL_CONFIG_PATH}[/bold cyan]",
                border_style="cyan"
            ))
            if click.confirm("Local config not found. Would you like to create 'langsync.json' here by copying the global config?", default=False):
                console.print("[green]➤ Selected: [bold]Yes[/bold][/green]")
                save_config('langsync.json', json.loads(global_content))
                console.print("[green]✓ Successfully created 'langsync.json' from global config.")
                console.print("[yellow]! Please update the paths and values in 'langsync.json' and run langsync again.")
                sys.exit(0)
            console.print("[cyan]➤ Selected: [bold]No[/bold]. Proceeding with global configuration...[/cyan]")
        except (OSError, ValueError) as e:
            console.print(f"[bold red]✖ Could not read the global config: {e}[/bold red]")
            _print_fixes(
                ("Inspect it:", f"python -m json.tool {GLOBAL_CONFIG_PATH}"),
                ("Or bypass it with an explicit config:", "langsync --config ./langsync.json"),
            )
        return

    detected = detect_project()
    starter = get_starter_config()
    if detected:
        for key in ("source", "dir", "pattern", "source_locale"):
            starter.pop(key, None)
        starter = dict(detected, **starter)

    if not interactive:
        _die(
            "No langsync.json found.",
            ("Create one" + (" (layout detected below)" if detected else "") + ":",
             "langsync   # in a terminal, answer 'y' at the prompt"),
            ("Or skip the config entirely:", "langsync --source ./messages/en.json --dir ./messages"),
            detail=("Detected: " + json.dumps(detected)) if detected else None,
        )

    if detected:
        console.print(Panel(
            JSON(json.dumps(starter)),
            title="[bold cyan]Detected your locale files[/bold cyan]",
            border_style="cyan",
        ))
        prompt = "No configuration file found. Create 'langsync.json' with these settings?"
    else:
        prompt = "No configuration file found. Would you like to create a default 'langsync.json'?"
    if click.confirm(prompt, default=True):
        console.print("[green]➤ Selected: [bold]Yes[/bold][/green]")
        save_config('langsync.json', starter)
        console.print("[green]✓ Successfully created 'langsync.json'.")
        if detected:
            console.print("[yellow]! Check it, then run [bold]langsync --dry-run[/bold] to preview the sync.")
        else:
            console.print("[yellow]! Please update the paths and values in 'langsync.json' and run langsync again.")
        sys.exit(0)
    console.print("[red]➤ Selected: [bold]No[/bold][/red]")
    _die(
        "Configuration is required to run langsync.",
        ("Create one interactively:", "langsync   # answer 'y' at the prompt"),
        ("Or point at an existing config:", "langsync --config path/to/langsync.json"),
        ("Or skip the config entirely:", "langsync --source ./messages/en.json --dir ./messages"),
    )


def _build_layout(source, dir, config_data, cfg_label):
    """Resolve the flat (source + dir) or pattern layout, validating paths."""
    pattern = config_data.get("pattern")
    if pattern and not (source or dir):
        source_locale = config_data.get("source_locale") or "en"
        try:
            layout = PatternLayout(pattern, source_locale)
        except ValueError as e:
            _die(f"Invalid \"pattern\" in {cfg_label}: {e}",
                 'Example: [bold]"pattern": "public/locales/{locale}/{ns}.json", "source_locale": "en"[/bold]')
        units = layout.source_units()
        if not units:
            source_glob = layout.target_path(source_locale, "*")
            _die(
                f"No source files match '{source_glob}'.",
                ("See what is actually there:", f"ls {os.path.dirname(source_glob) or '.'}"),
                f'Then fix [bold]"pattern"[/bold] or [bold]"source_locale"[/bold] in [bold]{cfg_label}[/bold].',
            )
        return layout, units

    source = source or config_data.get('source')
    dir = dir or config_data.get('dir')

    if not source:
        _die(
            "No source file configured.",
            ("Pass it on the command line:", "langsync --source ./messages/en.json"),
            f'Or set [bold]"source"[/bold] in [bold]{cfg_label}[/bold].',
        )

    if not os.path.exists(source):
        _die(
            f"Source file '{source}' not found.",
            ("See what is actually there:", f"ls -l {os.path.dirname(source) or '.'}"),
            f'Then fix [bold]"source"[/bold] in [bold]{cfg_label}[/bold], or pass [bold cyan]--source <path>[/bold cyan].',
        )

    if not os.path.isfile(source):
        _die(
            f"Source '{source}' exists but is not a regular file.",
            ("Check what it is:", f"ls -ld {source}"),
            "[bold]--source[/bold] must point at a single locale file, not a directory.",
        )

    ext = os.path.splitext(source)[1].lower()
    if ext not in fileio.SUPPORTED_EXTENSIONS:
        _die(
            f"Unsupported source format '{ext or source}'.",
            f"langsync reads {', '.join(fileio.SUPPORTED_EXTENSIONS)} files.",
        )

    if not dir:
        _die(
            "No locale directory configured.",
            ("Pass it on the command line:", "langsync --dir ./messages"),
            f'Or set [bold]"dir"[/bold] in [bold]{cfg_label}[/bold].',
        )

    if not os.path.exists(dir):
        _die(
            f"Locale directory '{dir}' not found.",
            ("Create it and seed a locale file:", f"mkdir -p {dir} && echo '{{}}' > {os.path.join(dir, 'fr-FR' + ext)}"),
            f'Or fix [bold]"dir"[/bold] in [bold]{cfg_label}[/bold].',
        )

    if not os.path.isdir(dir):
        _die(
            f"'{dir}' is not a directory.",
            ("Check what it is:", f"ls -ld {dir}"),
            "[bold]--dir[/bold] must point at the folder holding your per-locale files.",
        )

    layout = FlatLayout(source, dir)
    return layout, layout.source_units()


def _load_sources(units):
    for unit in units:
        try:
            unit.data, unit.style = fileio.load(unit.path)
        except json.JSONDecodeError as e:
            _die(
                f"Source file '{unit.path}' is not valid JSON.",
                ("Find the exact offending line:", f"python -m json.tool {unit.path}"),
                detail=e,
            )
        except fileio.FormatError as e:
            _die(f"Could not read source file '{unit.path}': {e}")
        except OSError as e:
            _die(
                f"Could not read source file '{unit.path}': {e}",
                ("Check permissions:", f"ls -l {unit.path}"),
            )

        if not isinstance(unit.data, dict):
            _die(
                f"Source file '{unit.path}' must contain an object at the top level "
                f"(found {type(unit.data).__name__}).",
                ("Inspect the top level:", f"head {unit.path}"),
                'Wrap the content in an object, e.g. [bold]{"app": {"title": "Hello"}}[/bold].',
            )

        if getattr(unit.style, "duplicates", None):
            console.print(
                f"[bold yellow]⚠ {unit.path} defines {len(unit.style.duplicates)} key(s) twice "
                f"({', '.join(unit.style.duplicates[:3])}); only the last value of each is used.[/bold yellow]"
            )


def _report_invalid_sources(units):
    """Source strings that look like ICU plural/select but do not parse are
    excluded from translation everywhere (translating them through the marker
    path is exactly what corrupts reserved keywords). Warn once."""
    invalid = []
    for unit in units:
        for path, value in LocaleProcessor.find_invalid_icu_sources(unit.data):
            invalid.append((unit, path, value))
    if not invalid:
        return invalid

    def _icu_error(value):
        try:
            icu.parse(value)
        except icu.ICUParseError as e:
            return str(e)
        return "unknown error"

    errors = [(unit.prefix + path_to_key(path), _icu_error(val)) for unit, path, val in invalid]
    lines = [f"[red]•[/red] [blue]{key}[/blue] — [dim]{err}[/dim]" for key, err in errors[:10]]
    if len(invalid) > 10:
        lines.append(f"[dim]... and {len(invalid) - 10} more[/dim]")
    console.print(Panel(
        "\n".join(lines) + "\n[yellow]These keys are skipped in every locale until the source is fixed.[/yellow]",
        title=f"[bold red]✖ {len(invalid)} invalid ICU MessageFormat source string(s)[/bold red]",
        border_style="red",
        expand=False,
    ))

    # Broken *source* ICU is the one case langsync cannot repair itself —
    # pushing it through the translator is exactly what corrupts reserved
    # keywords. Show only the edits the reported errors actually call for.
    all_errors = " | ".join(err for _, err in errors)
    files = sorted({unit.path for unit, _, _ in invalid})
    fixes = [f"Edit [bold]{', '.join(files)}[/bold] by hand — langsync will not translate these keys."]
    if "mandatory 'other' branch" in all_errors:
        fixes.append(
            "Every plural/select needs an [bold]other[/bold] branch:\n"
            "  [red]{n, plural, one {# item}}[/red]\n"
            "  [green]{n, plural, one {# item} other {# items}}[/green]"
        )
    if "selector" in all_errors:
        fixes.append(
            "Branch keywords are reserved — they stay English, only the text between "
            "the braces is translated:\n"
            "  [red]{n, plural, one {# Artikel} andere {# Artikel}}[/red]\n"
            "  [green]{n, plural, one {# Artikel} other {# Artikel}}[/green]"
        )
    if "unterminated" in all_errors or "unmatched" in all_errors or "expected" in all_errors:
        fixes.append(
            "Unbalanced braces — every [bold]{[/bold] needs its [bold]}[/bold]:\n"
            "  [red]{n, plural, one {# item} other {# items}[/red]\n"
            "  [green]{n, plural, one {# item} other {# items}}[/green]"
        )
    fixes.append(
        "A literal brace in prose must be quoted:\n"
        "  [red]Use {braces} here[/red]\n"
        "  [green]Use '{'braces'}' here[/green]"
    )
    fixes.append(("Re-check once you have edited the source:", "langsync --check"))
    fixes.append(
        "Fixing a source value changes its hash, so the next run re-translates that key in every locale."
    )
    _print_fixes(*fixes, title="How to fix invalid ICU source strings")
    return invalid


def _load_baseline(state_path, units, layout):
    """Return (snapshot, origin). On a first run inside a git repository, seed
    the snapshot from the sources as they were at the last commit touching the
    locale files, so an existing project gets drift detection from day one
    without re-translating every key."""
    snapshot = load_snapshot(state_path)
    if snapshot.exists:
        return snapshot, "snapshot", True
    in_git = is_inside_git_repo()
    if not in_git:
        return snapshot, "bootstrap", False
    hashes = {}
    for unit in units:
        try:
            baseline = find_baseline_source(unit.path, layout.default_state_dir())
        except Exception:
            baseline = None
        if baseline is None:
            continue
        try:
            for key, h in compute_source_hashes(baseline).items():
                hashes[unit.prefix + key] = h
        except Exception:
            continue
    if hashes:
        return Snapshot(hashes=hashes, behind={}, exists=False), "git", True
    return snapshot, "bootstrap", True


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('-s', '--source', help='Source locale file (defaults to the value in langsync.json).')
@click.option('-d', '--dir', help='Directory containing target locale files.')
@click.option('-l', '--locales', help='Comma-separated allowlist of locales to sync (e.g. "fr-FR,de-DE"). Defaults to every locale file found.')
@click.option('-a', '--add', 'add_locales', help='Comma-separated locales to create and sync (e.g. "ja,ko,pt-BR"). Their files are created if missing.')
@click.option('-c', '--config', help='Path to a langsync config JSON file.')
@click.option('-p', '--provider', type=click.Choice(PROVIDERS), help='Translation provider (default: google, or "provider" in langsync.json). deepl, libre and microsoft need an API key.')
@click.option('-r', '--rewrite', is_flag=True, help='Re-translate every key, ignoring the snapshot. Use sparingly — replaces all existing translations.')
@click.option('--update-changed', is_flag=True, help='Re-translate only keys whose source value changed since the last sync (skip missing keys). Mutually exclusive with --rewrite.')
@click.option('--prune', is_flag=True, help='Remove orphan keys (present in target locales but absent from source). Without it, orphans are reported but left in place.')
@click.option('--dry-run', is_flag=True, help='Classify keys and print what would change, without writing files or calling the translator.')
@click.option('--check', is_flag=True, help='Like --dry-run, but exit with code 1 if any locale has missing, changed, broken or orphan keys. Useful in CI.')
@click.option('--stats', is_flag=True, help='Show translation coverage per locale (writes nothing).')
@click.option('--json', 'json_output', is_flag=True, help='Print a machine-readable JSON report on stdout; the human output goes to stderr.')
@click.option('-y', '--yes', is_flag=True, help='Never prompt. Without a config, fail instead of asking (implied when stdin is not a terminal).')
@click.option('-v', '--verbose', is_flag=True, help='Print each translation, copy, and orphan path as it is processed.')
@click.version_option(__version__, prog_name="langsync")
def main(source, dir, locales, add_locales, config, provider, rewrite, update_changed, prune,
         dry_run, check, stats, json_output, yes, verbose):
    """Modern I18N sync tool with parallel translation and source-drift detection.

    On each run, langsync compares the source locale file against the per-locale
    target files and a `.langsync-state.json` snapshot of the previous sync. It
    classifies every key as one of:

        missing   — present in source, absent in target  → translates
        changed   — source value differs from the snapshot → re-translates
        orphan    — present in target, absent in source  → reports (or --prune)
        unchanged — snapshot hash matches the current source → skips

    On first run, if no snapshot exists, langsync tries to seed one from the
    source file as it was at the last commit touching the locale dir; failing
    that, it assumes everything is already in sync.

    Examples:

        \b
        # Translate missing + changed keys (default)
        langsync

        \b
        # Preview the work without making any changes
        langsync --dry-run

        \b
        # Start two new languages
        langsync --add ja,ko

        \b
        # Refresh only translations whose source copy was edited
        langsync --update-changed

        \b
        # Drop stale keys that no longer exist in the source
        langsync --prune

        \b
        # How complete is each locale?
        langsync --stats
    """
    signal.signal(signal.SIGINT, handle_sigint)
    # With --json, stdout carries only the report; everything human goes to
    # stderr. None restores rich's default of following the current sys.stdout.
    console.file = sys.stderr if json_output else None
    poll_update = start_update_check() if not json_output else (lambda _t: None)

    if rewrite and update_changed:
        _die(
            "--rewrite and --update-changed are mutually exclusive.",
            ("Re-translate every key (replaces existing translations):", "langsync --rewrite"),
            ("Re-translate only keys whose source text changed:", "langsync --update-changed"),
            ("Not sure which you want? Preview first:", "langsync --dry-run -v"),
            code=2,
        )

    # --check and --stats are stricter --dry-runs; reuse that code path.
    if check or stats:
        dry_run = True

    try:
        start_time = time.time()

        _ensure_config(config, source, dir, _interactive(yes, json_output))
        if config and not os.path.isfile(config):
            _die(
                f"Config file '{config}' not found.",
                ("See what is there:", f"ls -l {os.path.dirname(config) or '.'}"),
            )

        config_data, loaded_path = load_config(config)
        cfg_label = _config_label(loaded_path)
        rewrite = rewrite or bool(config_data.get('rewrite', False))
        if rewrite and update_changed:
            _die(
                f'"rewrite" is on in {cfg_label}, which conflicts with --update-changed.',
                f'Remove [bold]"rewrite"[/bold] from [bold]{cfg_label}[/bold] — use the --rewrite flag for one-off runs.',
                code=2,
            )

        layout, units = _build_layout(source, dir, config_data, cfg_label)
        _load_sources(units)

        if not any(unit.data for unit in units):
            console.print(
                f"[bold yellow]⚠ Source file '{units[0].path}' is empty — nothing to translate.[/bold yellow]"
            )
            _print_fixes(
                ("Add keys to the source, then preview the work:", "langsync --dry-run -v"),
                ("Every target key is an orphan while the source is empty — do not run:", "langsync --prune"),
                title="What to do next",
            )

        invalid_sources = _report_invalid_sources(units)

        provider = provider or config_data.get("provider") or "google"
        source_locale = layout.source_locale
        source_lang = config_data.get("source_lang") or (
            resolve_language(source_locale, provider, config_data.get("language_map"))
            if source_locale else "auto"
        )

        state_path = config_data.get('state_file') or os.path.join(layout.default_state_dir(), STATE_FILENAME)
        snapshot, baseline_origin, in_git_repo = _load_baseline(state_path, units, layout)

        try:
            discovered = layout.discover_locales()
        except OSError as e:
            _die(f"Could not list locale files: {e}", ("Check permissions:", f"ls -ld {layout.describe()}"))

        config_locales = [l for l in (config_data.get("locales") or []) if isinstance(l, str) and l.strip()]
        added = _split_locales(add_locales)
        bad_codes = [loc for loc in added if not looks_like_locale(loc)]
        if bad_codes:
            _die(
                f"--add got something that is not a locale code: {', '.join(bad_codes)}",
                "Use codes like [bold]fr[/bold], [bold]pt-BR[/bold] or [bold]zh-Hant[/bold].",
                code=2,
            )

        declared = set(discovered) | set(config_locales) | set(added)
        if locales:
            target_locales = _split_locales(locales)
            unknown = [loc for loc in target_locales if loc not in declared]
            if unknown:
                example = layout.target_path(unknown[0], units[0].ns)
                _die(
                    f"--locales referenced locales that have no files yet: {', '.join(unknown)}",
                    ("List the locales you actually have:", f"ls {os.path.dirname(example) or '.'}"),
                    ("Or create the missing one and sync it:", f"langsync --add {','.join(unknown)}"),
                )
            target_locales += [loc for loc in added if loc not in target_locales]
        else:
            target_locales = sorted(declared)
        if source_locale:
            target_locales = [loc for loc in target_locales if loc != source_locale]
        all_locales = (declared | set(target_locales)) - ({source_locale} if source_locale else set())

        if not target_locales:
            example = layout.target_path("fr", units[0].ns)
            console.print(f"[bold yellow]⚠ No target locale files found for '{layout.describe()}'.[/bold yellow]")
            _print_fixes(
                ("Add your first languages and sync them:", "langsync --add fr,de"),
                f"Or create [bold]{example}[/bold] yourself — the file name is the locale code.",
                title="What to do next",
            )
            if json_output:
                click.echo(json.dumps({"version": __version__, "in_sync": True, "locales": []}, indent=2))
            return

        opts = RunOptions(
            rewrite=rewrite,
            prune=prune,
            update_changed=update_changed,
            dry_run=dry_run,
            verbose=verbose,
            source_lang=source_lang,
            provider=provider,
            api_key=_api_key(config_data, provider),
            key_order=config_data.get("key_order") or "keep",
        )

        table = Table(box=None, padding=(0, 2))
        table.add_column("Property", style="bold blue")
        table.add_column("Value", style="white")

        table.add_row("Version", f"[magenta]{__version__}[/magenta]")
        if loaded_path:
            table.add_row("Config", f"[cyan]{loaded_path}[/cyan]")
        if layout.kind == "flat":
            table.add_row("Source", f"[green]{units[0].path}[/green]")
            table.add_row("Directory", f"[green]{layout.dir}[/green]")
        else:
            table.add_row("Pattern", f"[green]{layout.pattern}[/green]")
            ns_names = [u.ns for u in units if u.ns is not None]
            table.add_row(
                "Source",
                f"[green]{source_locale}[/green]"
                + (f" [dim]({len(units)} files: {', '.join(ns_names[:4])}{'…' if len(ns_names) > 4 else ''})[/dim]" if ns_names else ""),
            )
        table.add_row("Provider", f"[cyan]{provider}[/cyan] [dim]({source_lang} →)[/dim]")
        if baseline_origin == "snapshot":
            snapshot_state = f"[green]{state_path}[/green]"
        elif baseline_origin == "git":
            snapshot_state = (
                f"[yellow]{state_path}[/yellow] "
                f"[dim](first run — baseline derived from git history)[/dim]"
            )
        else:
            snapshot_state = (
                f"[yellow]{state_path}[/yellow] "
                f"[dim](first run — bootstrapping from current source)[/dim]"
            )
        table.add_row("Snapshot", snapshot_state)
        table.add_row("Locales", f"[yellow]{len(target_locales)}[/yellow] ({', '.join(target_locales[:5])}{'...' if len(target_locales) > 5 else ''})")

        status_flags = []
        if rewrite: status_flags.append("[bold red]Rewrite[/bold red]")
        if update_changed: status_flags.append("[bold yellow]Update-Changed[/bold yellow]")
        if prune: status_flags.append("[bold magenta]Prune[/bold magenta]")
        if check: status_flags.append("[bold red]Check[/bold red]")
        elif stats: status_flags.append("[bold cyan]Stats[/bold cyan]")
        elif dry_run: status_flags.append("[bold yellow]Dry-Run[/bold yellow]")
        if added: status_flags.append(f"[bold green]Add {','.join(added)}[/bold green]")
        if verbose: status_flags.append("[bold cyan]Verbose[/bold cyan]")

        table.add_row("Mode", " + ".join(status_flags) if status_flags else "[dim]Standard[/dim]")

        console.print(Panel(table, title="[bold white]Settings Summary[/bold white]", border_style="blue", expand=False))

        # First-run + no-git tip: surfaces *once* (subsequent runs will have a snapshot).
        if baseline_origin == "bootstrap" and not in_git_repo:
            console.print(
                "[dim]💡 Tip:[/dim] [yellow]This project isn't a git repository.[/yellow] "
                "Initialize one with [cyan]git init[/cyan] so future first-runs can derive a "
                "drift baseline from history."
            )

        results = []
        max_parallel_locales = max(1, int(config_data.get('max_parallel_locales') or 3))

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None, pulse_style="cyan"),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            expand=True
        ) as progress:
            main_task_id = progress.add_task("[bold green]Total Progress", total=len(target_locales))

            with ThreadPoolExecutor(max_workers=max_parallel_locales) as locale_executor:
                futures = {
                    locale_executor.submit(
                        process_locale, locale, units, layout, progress, main_task_id, config_data,
                        snapshot=snapshot, opts=opts,
                    ): locale
                    for locale in target_locales
                }

                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        crash = LocaleResult(futures[future])
                        crash.failed_prefixes.update(unit.prefix for unit in units)
                        crash.add_issue("crash", f"locale worker crashed: {e}")
                        results.append(crash)

        sorted_results = sorted(results, key=lambda x: x.locale)

        # Aggregate before building the table so we can hide zero columns.
        total_translated = sum(r.translated for r in sorted_results)
        total_copied = sum(r.copied for r in sorted_results)
        total_failed = sum(r.failed for r in sorted_results)
        total_pruned = sum(r.pruned for r in sorted_results)
        total_missing = sum(r.missing_count for r in sorted_results)
        total_changed = sum(r.changed_count for r in sorted_results)
        total_orphans = sum(r.orphan_count for r in sorted_results)
        total_invalid = sum(r.invalid_count for r in sorted_results)
        total_io = sum(len(r.failed_prefixes) for r in sorted_results)
        total_suspects = sum(len(r.suspects) for r in sorted_results)

        if stats:
            stats_table = Table(title="\n[bold cyan]Translation Coverage[/bold cyan]", box=None,
                                header_style="bold underline white")
            stats_table.add_column("Locale", style="cyan")
            stats_table.add_column("Coverage", justify="right")
            stats_table.add_column("Strings", justify="right", style="dim")
            stats_table.add_column("Missing", justify="right", style="yellow")
            stats_table.add_column("Outdated", justify="right", style="red")
            stats_table.add_column("Same as source", justify="right", style="bright_black")
            stats_table.add_column("Placeholder issues", justify="right", style="bright_red")
            for r in sorted_results:
                pct = r.coverage
                color = "green" if pct >= 99.95 else ("yellow" if pct >= 80 else "red")
                stats_table.add_row(
                    r.locale, f"[{color}]{pct:.1f}%[/{color}]", str(r.total_strings),
                    str(r.missing_translatable or "—"), str(r.changed_count or "—"),
                    str(r.identical_count or "—"), str(len(r.suspects) or "—"),
                )
            console.print(stats_table)
            console.print(
                "[dim]Same as source: the translation equals the source text — fine for names and "
                "\"OK\", otherwise usually never translated. See them with [cyan]langsync --dry-run -v[/cyan].[/dim]"
            )

        # Decide which columns to render — drop ones that would be uniformly "—".
        show_missing = total_missing > 0
        show_changed = total_changed > 0
        show_invalid = total_invalid > 0
        show_orphans = total_orphans > 0
        show_unchanged = verbose  # noise unless the user asked for detail
        show_translated = (not dry_run) and total_translated > 0
        show_copied = (not dry_run) and total_copied > 0
        show_pruned = (not dry_run) and total_pruned > 0
        show_failed = (not dry_run) and total_failed > 0

        if dry_run:
            summary_title = "\n[bold yellow]Dry Run Statistics[/bold yellow]"
        else:
            summary_title = "\nSync Statistics"
        summary_table = Table(title=summary_title, box=None, header_style="bold underline white")
        summary_table.add_column("Locale", style="cyan")
        summary_table.add_column("Status")
        if show_missing:   summary_table.add_column("Missing",    justify="right", style="yellow")
        if show_changed:   summary_table.add_column("Changed",    justify="right", style="red")
        if show_invalid:   summary_table.add_column("Broken ICU", justify="right", style="bright_red")
        if show_orphans:   summary_table.add_column("Orphans",    justify="right", style="bright_black")
        if show_unchanged: summary_table.add_column("Unchanged",  justify="right", style="dim")
        if show_translated:summary_table.add_column("Translated", justify="right", style="green")
        if show_copied:    summary_table.add_column("Copied",     justify="right", style="magenta")
        if show_pruned:    summary_table.add_column("Pruned",     justify="right", style="bright_magenta")
        if show_failed:    summary_table.add_column("Failed",     justify="right", style="red")

        def _cell(value, bold=False):
            if not value:
                return "—"
            return f"[bold]{value}[/bold]" if bold else f"{value}"

        for r in sorted_results:
            if dry_run:
                pending = r.missing_count + r.changed_count + r.invalid_count
                if r.failed_prefixes:
                    status_text = "[red]Unreadable[/red]"
                elif pending > 0:
                    status_text = "[yellow]Pending[/yellow]"
                elif r.orphan_count > 0:
                    status_text = "[bright_black]Orphans only[/bright_black]"
                else:
                    status_text = "[dim]Up to date[/dim]"
            else:
                status_text = {
                    "done": "[green]Done[/green]" if not r.created else "[green]Created[/green]",
                    "partial": "[yellow]Partial[/yellow]",
                    "failed": "[red]Failed[/red]",
                    "uptodate": "[dim]Up to date[/dim]",
                }[r.status]

            row = [r.locale, status_text]
            if show_missing:    row.append(_cell(r.missing_count, bold=True))
            if show_changed:    row.append(_cell(r.changed_count, bold=True))
            if show_invalid:    row.append(_cell(r.invalid_count, bold=True))
            if show_orphans:    row.append(_cell(r.orphan_count, bold=True))
            if show_unchanged:  row.append(_cell(r.unchanged_count))
            if show_translated: row.append(_cell(r.translated))
            if show_copied:     row.append(_cell(r.copied))
            if show_pruned:     row.append(_cell(r.pruned))
            if show_failed:     row.append(_cell(r.failed))

            summary_table.add_row(*row)

        # If literally nothing happened, skip the table entirely and rely on the footer.
        nothing_to_show = stats or (
            dry_run and total_missing == 0 and total_changed == 0 and total_orphans == 0
            and total_invalid == 0 and total_io == 0
        ) or (
            not dry_run and not (
                show_translated or show_copied or show_pruned or show_failed
                or total_missing or total_changed or total_orphans or total_invalid
                or any(r.created for r in sorted_results)
            )
        )
        if not nothing_to_show:
            console.print(summary_table)

        issues_panel = _render_issues_panel(results)
        if issues_panel:
            console.print()
            console.print(issues_panel)
            _print_fixes(*_issue_fixes(results, cfg_label, layout, provider), title="Suggested fixes")

        if total_suspects:
            lines = []
            for r in sorted_results:
                for key, problem in r.suspects:
                    lines.append(rf"[cyan]\[{r.locale}][/cyan] [blue]{key}[/blue] — {problem}")
            shown = lines[:12]
            if len(lines) > 12:
                shown.append(f"[dim]... and {len(lines) - 12} more (see --json)[/dim]")
            console.print()
            console.print(Panel(
                "\n".join(shown),
                title=f"[bold bright_red]⚠ {total_suspects} existing translation(s) do not match their source's placeholders[/bold bright_red]",
                border_style="bright_red",
                expand=False,
            ))
            _print_fixes(
                "These were left untouched — a placeholder that is missing or renamed shows up "
                "as raw text or nothing at runtime. Fix them by hand, or re-translate them by "
                "deleting the key from that locale file and running:",
                ("", "langsync"),
                title="Placeholder problems",
            )

        # Orphan warning — default behavior leaves them alone, so call it out.
        if total_orphans > 0 and not prune:
            orphan_msg = (
                f"[bold yellow]⚠ {total_orphans} orphan key(s)[/bold yellow] "
                f"(present in target locales but absent from source) were left in place."
            )
            console.print()
            console.print(Panel(orphan_msg, border_style="yellow", expand=False))
            _print_fixes(
                ("See exactly which keys they are:", "langsync --dry-run -v"),
                ("Remove them once you are happy with the list:", "langsync --prune"),
                "Leaving them is safe — they are only reported, never translated.",
                title="What to do about orphans",
            )

        # Persist a fresh snapshot. Each locale only advances the keys it really
        # synced: a locale skipped by --locales, a failed key, or an unreadable
        # file stays "behind" so its next run still sees the source edits.
        if not dry_run:
            try:
                current_hashes = _prefixed_hashes(units)
                failed = {}
                for r in results:
                    keys = set(r.failed_paths)
                    for prefix in r.failed_prefixes:
                        keys.update(k for k in current_hashes if k.startswith(prefix))
                    if keys:
                        failed[r.locale] = keys
                new_snapshot = advance_snapshot(
                    snapshot, current_hashes,
                    processed={r.locale for r in results},
                    all_locales=all_locales,
                    failed=failed,
                )
                save_snapshot(state_path, new_snapshot)
            except OSError as e:
                console.print(f"[bold yellow]⚠ Could not write snapshot {state_path}: {e}[/bold yellow]")
                _print_fixes(
                    ("Make the directory writable, then re-run:", f"ls -ld {os.path.dirname(state_path) or '.'}"),
                    "Until the snapshot is written, drift detection falls back to the previous baseline "
                    "and already-translated keys may be re-translated next run.",
                )
            except Exception as e:
                console.print(f"[bold yellow]⚠ Snapshot update skipped: {e}[/bold yellow]")
                _print_fixes(
                    ("Re-run to rebuild the snapshot:", "langsync"),
                    ("If it keeps failing, delete the snapshot and let langsync rebuild it:",
                     f"rm {state_path} && langsync --dry-run"),
                )

        total_time = time.time() - start_time
        console.print()

        # Invalid source strings and unreadable locale files count as drift:
        # --check exits 1 on them, so the headline must not claim "in sync".
        drift_total = (
            total_missing + total_changed + total_orphans + total_invalid
            + len(invalid_sources) + total_io
        )

        if dry_run:
            next_steps = []
            if total_missing > 0 and total_changed == 0 and total_orphans == 0:
                next_steps.append("[cyan]langsync[/cyan] translates the missing keys.")
            elif total_changed > 0 and total_missing == 0:
                next_steps.append("[cyan]langsync --update-changed[/cyan] re-translates only the edited keys.")
            elif total_missing > 0 and total_changed > 0:
                next_steps.append("[cyan]langsync[/cyan] handles missing + changed keys.")
            if total_invalid > 0:
                next_steps.append(
                    f"[cyan]langsync[/cyan] re-translates the {total_invalid} broken ICU string(s) in place "
                    "(no flag needed — healing overrides the snapshot)."
                )
            if total_orphans > 0:
                next_steps.append("[cyan]langsync --prune[/cyan] removes the orphan keys.")
            if total_io > 0:
                next_steps.append(
                    "[bright_red]Fix the unreadable locale file(s)[/bright_red] listed under Issues above."
                )
            if invalid_sources:
                next_steps.append(
                    f"[bright_red]Edit the source by hand[/bright_red] to fix the {len(invalid_sources)} "
                    "invalid ICU source string(s) — langsync skips them until then."
                )
            next_steps_text = ("\n" + "\n".join(f"[dim]→[/dim] {s}" for s in next_steps)) if next_steps else ""

            if check and drift_total:
                headline = "[bold red]✖ Drift detected[/bold red]"
                border_style = "red"
            elif check:
                headline = "[bold green]✓ All locales are in sync[/bold green]"
                border_style = "green"
            elif drift_total:
                headline = "[bold yellow]⚠ Dry Run — pending work[/bold yellow]"
                border_style = "yellow"
            else:
                headline = "[bold green]✓ Dry Run — nothing to do[/bold green]"
                border_style = "green"

            footer_text = (
                f"{headline}\n"
                f"[dim]Time elapsed:[/dim] [bold cyan]{total_time:.2f}s[/bold cyan]\n"
                f"[dim]Missing:[/dim] [bold yellow]{total_missing}[/bold yellow]   "
                f"[dim]Changed:[/dim] [bold red]{total_changed}[/bold red]   "
                + (f"[dim]Broken ICU:[/dim] [bold bright_red]{total_invalid}[/bold bright_red]   " if total_invalid else "")
                + f"[dim]Orphans:[/dim] [bold bright_black]{total_orphans}[/bold bright_black]"
                f"{next_steps_text}"
            )
        else:
            if total_failed > 0 or total_io > 0:
                headline = "[bold yellow]⚠ Sync Completed With Issues[/bold yellow]"
                border_style = "yellow"
                retry_cmd = _retry_command(results)
                tail = (
                    f"\n[dim]The snapshot was not advanced for the [bold red]{total_failed}[/bold red] "
                    f"failed key(s), so nothing was lost"
                    + (f" — a re-run picks them up:[/dim] [bold cyan]{retry_cmd}[/bold cyan]"
                       if retry_cmd else ".[/dim] [dim]See the suggested fixes above.[/dim]")
                )
            else:
                headline = "[bold green]✓ Sync Completed Successfully![/bold green]"
                border_style = "green"
                tail = ""

            created = [path for r in sorted_results for path in r.created]
            if created:
                tail += (
                    f"\n[dim]Created[/dim] [bold green]{len(created)}[/bold green] [dim]new locale file(s).[/dim]"
                )
            if total_invalid > 0:
                tail += (
                    f"\n[dim]Healed[/dim] [bold bright_red]{total_invalid}[/bold bright_red] "
                    f"[dim]broken ICU string(s) that were already in your locale files.[/dim]"
                )
            if invalid_sources:
                tail += (
                    f"\n[dim]Skipped[/dim] [bold bright_red]{len(invalid_sources)}[/bold bright_red] "
                    f"[dim]invalid ICU source string(s) — fix the source by hand, then re-run.[/dim]"
                )

            footer_text = (
                f"{headline}\n"
                f"[dim]Time elapsed:[/dim] [bold cyan]{total_time:.2f}s[/bold cyan]\n"
                f"[dim]Translated:[/dim] [bold green]{total_translated}[/bold green]   "
                f"[dim]Copied:[/dim] [bold magenta]{total_copied}[/bold magenta]   "
                f"[dim]Pruned:[/dim] [bold bright_magenta]{total_pruned}[/bold bright_magenta]   "
                f"[dim]Failed:[/dim] [bold red]{total_failed}[/bold red]"
                f"{tail}"
            )

        if not stats or check:
            console.print(Panel(footer_text, border_style=border_style, expand=False))

        if json_output:
            report = {
                "version": __version__,
                "mode": "check" if check else ("stats" if stats else ("dry-run" if dry_run else "sync")),
                "provider": provider,
                "source_lang": source_lang,
                "in_sync": drift_total == 0,
                "elapsed_seconds": round(total_time, 2),
                "totals": {
                    "missing": total_missing, "changed": total_changed, "broken_icu": total_invalid,
                    "orphans": total_orphans, "translated": total_translated, "copied": total_copied,
                    "pruned": total_pruned, "failed": total_failed,
                    "placeholder_problems": total_suspects,
                },
                "invalid_sources": [
                    {"file": unit.path, "key": unit.prefix + path_to_key(path)}
                    for unit, path, _ in invalid_sources
                ],
                "locales": [r.to_dict() for r in sorted_results],
            }
            click.echo(json.dumps(report, indent=2, ensure_ascii=False))

        _print_update_banner(poll_update(0.4))

        if check and drift_total > 0:
            console.print("[dim]--check exits 1 because the locales are not in sync (see the steps above).[/dim]")
            sys.exit(1)
        if not dry_run and (total_failed or total_io):
            # A partial sync is not a success for scripts and CI.
            sys.exit(1)

    except KeyboardInterrupt:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()
