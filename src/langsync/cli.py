import os
import sys
import json
import click
import time
import signal
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.panel import Panel
from rich.json import JSON

from .translator import TranslationService, TranslationError, get_translator_code
from .processor import LocaleProcessor
from .config import load_config, GLOBAL_CONFIG_PATH, LOCAL_CONFIG_NAMES, get_default_config, save_config
from .state import (
    STATE_FILENAME,
    compute_source_hashes,
    default_state_path,
    load_state,
    path_to_key,
    save_state,
)
from .git_baseline import find_baseline_source, is_inside_git_repo
from .update_check import start_update_check
from . import __version__

console = Console()


def handle_sigint(signum, frame):
    """Gracefully handle Ctrl+C."""
    console.print("\n[bold red]✖ Interrupted by user. Exiting...[/bold red]")
    sys.exit(0)


class LocaleResult:
    """Outcome of processing a single locale. Holds counts and human-readable issues."""

    __slots__ = (
        "locale", "translated", "copied", "failed", "pruned",
        "missing_count", "changed_count", "orphan_count", "unchanged_count",
        "issues", "failed_paths",
    )

    def __init__(self, locale):
        self.locale = locale
        self.translated = 0
        self.copied = 0
        self.failed = 0
        self.pruned = 0
        self.missing_count = 0
        self.changed_count = 0
        self.orphan_count = 0
        self.unchanged_count = 0
        self.issues = []  # list of (kind, message)
        self.failed_paths = set()  # dotted-path strings the run could not sync

    def add_issue(self, kind, message):
        self.issues.append((kind, message))

    def mark_failed(self, path):
        self.failed += 1
        self.failed_paths.add(path_to_key(path))

    @property
    def status(self):
        if self.failed and self.translated == 0 and self.copied == 0 and self.pruned == 0:
            return "failed"
        if self.failed:
            return "partial"
        if self.translated or self.copied or self.pruned:
            return "done"
        return "uptodate"


def _format_path(path):
    return ".".join(str(p) for p in path)


def _translate_with_fallback(translator_service, batch, retry_count, delay, result, progress, locale_task_id, verbose):
    """Translate a batch. If batch fails, fall back to per-item translation.
    Mutates `result` (counts + issues). Returns list of (path, translated_value)
    for successfully translated items.
    """
    paths = [item[0] for item in batch]
    values = [item[1] for item in batch]

    translated_values = None
    last_batch_error = None

    current_delay = delay
    for attempt in range(retry_count):
        try:
            translated_values = translator_service.translate_batch(values, current_delay)
            if translated_values and len(translated_values) == len(values):
                break
        except Exception as e:
            last_batch_error = e
            if "RATE_LIMIT_HIT" in str(e):
                cooldown = 2 * (attempt + 1)
                time.sleep(cooldown)
                current_delay = min(current_delay * 2, 2.0)
            else:
                time.sleep(1 * (attempt + 1))
            translated_values = None

    succeeded = []

    if translated_values and len(translated_values) == len(values):
        per_item_failures = []
        for path, src_val, trans_val in zip(paths, values, translated_values):
            if trans_val is None or trans_val == "":
                per_item_failures.append((path, src_val))
            else:
                succeeded.append((path, trans_val))
                progress.update(locale_task_id, advance=1)
                if verbose:
                    progress.console.print(
                        rf"[dim]\[{result.locale}][/dim] Translated [blue]{_format_path(path)}[/blue] -> [italic]{trans_val}[/italic]"
                    )

        if per_item_failures:
            for path, src_val in per_item_failures:
                try:
                    trans_val = translator_service.translate_one(src_val, delay=current_delay)
                    if trans_val and trans_val != src_val:
                        succeeded.append((path, trans_val))
                        if verbose:
                            progress.console.print(
                                rf"[dim]\[{result.locale}][/dim] Translated [blue]{_format_path(path)}[/blue] -> [italic]{trans_val}[/italic]"
                            )
                    else:
                        result.mark_failed(path)
                        result.add_issue("empty", f"empty response for '{_format_path(path)}'")
                except TranslationError as e:
                    result.mark_failed(path)
                    result.add_issue(e.kind, f"'{_format_path(path)}': {e}")
                finally:
                    progress.update(locale_task_id, advance=1)
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

    for path, src_val in zip(paths, values):
        try:
            trans_val = translator_service.translate_one(src_val, delay=current_delay)
            if trans_val and trans_val != src_val:
                succeeded.append((path, trans_val))
                if verbose:
                    progress.console.print(
                        rf"[dim]\[{result.locale}][/dim] Translated [blue]{_format_path(path)}[/blue] -> [italic]{trans_val}[/italic]"
                    )
            else:
                result.mark_failed(path)
                result.add_issue("empty", f"empty response for '{_format_path(path)}'")
        except TranslationError as e:
            result.mark_failed(path)
            result.add_issue(e.kind, f"'{_format_path(path)}': {e}")
        finally:
            progress.update(locale_task_id, advance=1)

    return succeeded


def process_locale(
    locale, source_data, messages_dir, progress, main_task_id, config,
    *, snapshot_hashes, rewrite=False, prune=False, update_changed=False,
    dry_run=False, verbose=False,
):
    result = LocaleResult(locale)
    target_file = os.path.join(messages_dir, f"{locale}.json")

    try:
        target_data = LocaleProcessor.load_json(target_file)
    except json.JSONDecodeError as e:
        result.add_issue("io", f"{target_file} is not valid JSON ({e.msg}); skipping locale")
        progress.update(main_task_id, advance=1)
        return result
    except OSError as e:
        result.add_issue("io", f"failed to read {target_file}: {e}")
        progress.update(main_task_id, advance=1)
        return result

    if not isinstance(target_data, dict):
        result.add_issue(
            "io",
            f"{target_file} must contain a JSON object at the top level "
            f"(found {type(target_data).__name__}); skipping locale",
        )
        progress.update(main_task_id, advance=1)
        return result

    processor = LocaleProcessor(source_data)
    classification = processor.classify_keys(
        target_data,
        snapshot_hashes=snapshot_hashes,
        force_rewrite=rewrite,
    )

    missing_translatable = classification["missing_translatable"]
    missing_passthrough = classification["missing_passthrough"]
    changed_translatable = classification["changed_translatable"]
    changed_passthrough = classification["changed_passthrough"]
    unchanged_paths = classification["unchanged"]
    orphan_paths = classification["orphans"]

    # `--update-changed` narrows the run to drift-only; rewrite still wins.
    if update_changed and not rewrite:
        missing_translatable = []
        missing_passthrough = []

    translatable = missing_translatable + changed_translatable
    passthrough = missing_passthrough + changed_passthrough

    result.missing_count = len(classification["missing_translatable"]) + len(classification["missing_passthrough"])
    result.changed_count = len(classification["changed_translatable"]) + len(classification["changed_passthrough"])
    result.orphan_count = len(orphan_paths)
    result.unchanged_count = len(unchanged_paths)
    result.copied = len(passthrough)

    if dry_run:
        if verbose:
            for path, val in classification["missing_translatable"]:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [yellow]Missing:[/yellow] [blue]{_format_path(path)}[/blue] -> [italic]{val}[/italic]"
                )
            for path, val in classification["changed_translatable"]:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [red]Changed:[/red] [blue]{_format_path(path)}[/blue] -> [italic]{val}[/italic]"
                )
            for path, val in (missing_passthrough + changed_passthrough):
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [magenta]Copy as-is:[/magenta] [blue]{_format_path(path)}[/blue]"
                )
            for path in orphan_paths:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [bright_black]Orphan:[/bright_black] [blue]{_format_path(path)}[/blue]"
                )
        progress.update(main_task_id, advance=1)
        return result

    # Apply pass-through copies — no API calls needed.
    for path, val in passthrough:
        LocaleProcessor.set_value_by_path(target_data, path, val)

    # Optional orphan removal.
    if prune:
        for path in orphan_paths:
            LocaleProcessor.remove_by_path(target_data, path)
            result.pruned += 1

    if not translatable:
        try:
            LocaleProcessor.save_json(target_file, target_data)
        except Exception as e:
            result.add_issue("io", f"failed to write {target_file}: {e}")
        progress.update(main_task_id, advance=1)
        return result

    lang_code = get_translator_code(locale)
    try:
        translator_service = TranslationService(target_lang=lang_code, whitelist=config.get('whitelist'))
    except Exception as e:
        for path, _ in translatable:
            result.failed_paths.add(path_to_key(path))
        result.failed = len(translatable)
        result.add_issue("init", f"could not init translator for '{lang_code}': {e}")
        progress.update(main_task_id, advance=1)
        return result

    batch_size = config.get('batch_size', 25)
    delay = config.get('delay_between_requests', 0.2)
    retry_count = config.get('retry_count', 3)

    locale_task_id = progress.add_task(f"[cyan]{locale}", total=len(translatable))

    batches = [translatable[i:i + batch_size] for i in range(0, len(translatable), batch_size)]

    for batch in batches:
        succeeded = _translate_with_fallback(
            translator_service, batch, retry_count, delay, result,
            progress, locale_task_id, verbose,
        )
        for path, trans_val in succeeded:
            LocaleProcessor.set_value_by_path(target_data, path, trans_val)
            result.translated += 1

    try:
        LocaleProcessor.save_json(target_file, target_data)
    except Exception as e:
        result.add_issue("io", f"failed to write {target_file}: {e}")

    progress.remove_task(locale_task_id)
    progress.update(main_task_id, advance=1)
    return result


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

        sample_messages = [msg for _, msg in r.issues[:2]]
        if len(r.issues) > 2:
            sample_messages.append(f"... and {len(r.issues) - 2} more")
        details = "\n".join(sample_messages) if sample_messages else "—"

        table.add_row(
            r.locale,
            str(r.failed) if r.failed else "—",
            cause_summary or "—",
            details,
        )

    hints = []
    all_kinds = Counter(k for r in locales_with_issues for k, _ in r.issues)
    if all_kinds.get("rate_limit"):
        hints.append("• Rate-limited by Google Translate — increase `delay_between_requests` or lower `max_parallel_locales`.")
    if all_kinds.get("network"):
        hints.append("• Network errors detected — check your connection and re-run; failed keys will be retried.")
    if all_kinds.get("api"):
        hints.append("• Translator API returned errors for some items — re-run to retry, or rephrase the source string.")
    if all_kinds.get("empty"):
        hints.append("• Some items came back empty — these were left unset so the next run will retry them.")
    if all_kinds.get("io"):
        hints.append("• File I/O issues — verify the locale file is writable and not held open elsewhere.")
    hint_text = "\n".join(hints)

    panel_body = table
    if hint_text:
        return Panel.fit(
            table,
            title="[bold yellow]⚠ Issues encountered[/bold yellow]",
            border_style="yellow",
            subtitle=hint_text,
            subtitle_align="left",
        )
    return Panel.fit(
        panel_body,
        title="[bold yellow]⚠ Issues encountered[/bold yellow]",
        border_style="yellow",
    )


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


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('-s', '--source', help='Source JSON file (defaults to the value in langsync.json).')
@click.option('-d', '--dir', help='Directory containing target locale files.')
@click.option('-l', '--locales', help='Comma-separated allowlist of locales to sync (e.g. "fr-FR,de-DE"). Defaults to every JSON file in --dir.')
@click.option('-c', '--config', help='Path to a langsync config JSON file.')
@click.option('-r', '--rewrite', is_flag=True, help='Re-translate every key, ignoring the snapshot. Use sparingly — replaces all existing translations.')
@click.option('--update-changed', is_flag=True, help='Re-translate only keys whose source value changed since the last sync (skip missing keys). Mutually exclusive with --rewrite.')
@click.option('--prune', is_flag=True, help='Remove orphan keys (present in target locales but absent from source). Without it, orphans are reported but left in place.')
@click.option('--dry-run', is_flag=True, help='Classify keys and print what would change, without writing files or calling the translator.')
@click.option('--check', is_flag=True, help='Like --dry-run, but exit with code 1 if any locale has missing, changed, or orphan keys. Useful in CI.')
@click.option('-v', '--verbose', is_flag=True, help='Print each translation, copy, and orphan path as it is processed.')
@click.version_option(__version__, prog_name="langsync")
def main(source, dir, locales, config, rewrite, update_changed, prune, dry_run, check, verbose):
    """Modern I18N sync tool with parallel translation and source-drift detection.

    On each run, langsync compares the source JSON file against the per-locale
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
        # Refresh only translations whose source copy was edited
        langsync --update-changed

        \b
        # Drop stale keys that no longer exist in the source
        langsync --prune

        \b
        # Force re-translation of every key (replaces existing translations)
        langsync --rewrite
    """
    signal.signal(signal.SIGINT, handle_sigint)
    poll_update = start_update_check()

    if rewrite and update_changed:
        console.print(
            "[red]Error: --rewrite and --update-changed are mutually exclusive. "
            "--rewrite already re-translates every key.[/red]"
        )
        sys.exit(2)

    # --check is a stricter --dry-run; reuse the dry-run code path and assert at the end.
    if check:
        dry_run = True

    try:
        start_time = time.time()

        if not config:
            local_exists = any(os.path.exists(os.path.join(os.getcwd(), name)) for name in LOCAL_CONFIG_NAMES)
            if not local_exists:
                if os.path.exists(GLOBAL_CONFIG_PATH):
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
                            config_dict = json.loads(global_content)
                            save_config('langsync.json', config_dict)
                            console.print("[green]✓ Successfully created 'langsync.json' from global config.")
                            console.print("[yellow]! Please update the paths and values in 'langsync.json' and run langsync again.")
                            sys.exit(0)
                        else:
                            console.print("[cyan]➤ Selected: [bold]No[/bold]. Proceeding with global configuration...[/cyan]")
                    except Exception as e:
                        console.print(f"[red]Error handling global config: {e}")
                else:
                    if click.confirm("No configuration file found. Would you like to create a default 'langsync.json'?", default=True):
                        console.print("[green]➤ Selected: [bold]Yes[/bold][/green]")
                        save_config('langsync.json', get_default_config())
                        console.print("[green]✓ Successfully created default 'langsync.json'.")
                        console.print("[yellow]! Please update the paths and values in 'langsync.json' and run langsync again.")
                        sys.exit(0)
                    else:
                        console.print("[red]➤ Selected: [bold]No[/bold][/red]")
                        console.print("[red]Error: Configuration is required to run langsync.")
                        sys.exit(1)

        config_data, loaded_path = load_config(config)

        source = source or config_data.get('source')
        dir = dir or config_data.get('dir')
        rewrite = rewrite or config_data.get('rewrite', False)

        if not source:
            console.print("[red]Error: No source file configured. Provide it via --source or set 'source' in langsync.json.[/red]")
            sys.exit(1)

        if not os.path.exists(source):
            console.print(
                f"[red]Error: Source file '{source}' not found.[/red] "
                f"[dim]Check the path or update 'source' in your config.[/dim]"
            )
            sys.exit(1)

        if not os.path.isfile(source):
            console.print(f"[red]Error: Source '{source}' exists but is not a regular file.[/red]")
            sys.exit(1)

        if not dir:
            console.print("[red]Error: No locale directory configured. Provide it via --dir or set 'dir' in langsync.json.[/red]")
            sys.exit(1)

        if not os.path.exists(dir):
            console.print(
                f"[red]Error: Directory '{dir}' not found.[/red] "
                f"[dim]Create it and add per-locale JSON files (e.g. fr-FR.json), or fix the path.[/dim]"
            )
            sys.exit(1)

        if not os.path.isdir(dir):
            console.print(f"[red]Error: '{dir}' is not a directory.[/red]")
            sys.exit(1)

        try:
            source_data = LocaleProcessor.load_json(source)
        except json.JSONDecodeError as e:
            console.print(
                f"[red]Error: Source file '{source}' is not valid JSON.[/red]\n"
                f"[dim]{e}[/dim]"
            )
            sys.exit(1)
        except OSError as e:
            console.print(f"[red]Error reading source file '{source}': {e}[/red]")
            sys.exit(1)

        if not isinstance(source_data, dict):
            console.print(
                f"[red]Error: Source file '{source}' must contain a JSON object at the top level "
                f"(found {type(source_data).__name__}).[/red]"
            )
            sys.exit(1)

        if not source_data:
            console.print(
                f"[yellow]Warning: Source file '{source}' is empty. Nothing to translate.[/yellow]"
            )

        state_path = config_data.get('state_file') or default_state_path(dir)
        snapshot_hashes, snapshot_existed = load_state(state_path)
        baseline_origin = "snapshot" if snapshot_existed else "bootstrap"
        in_git_repo = is_inside_git_repo()

        # First-run UX: if there's no snapshot yet but the project lives in a
        # git repo, derive an initial baseline from the source as it was at the
        # last commit that touched the locale dir. This means an existing repo
        # adopting langsync gets real drift detection from day one without
        # re-translating every key.
        if not snapshot_existed and in_git_repo:
            try:
                git_baseline = find_baseline_source(source, dir)
            except Exception:
                git_baseline = None
            if git_baseline is not None:
                try:
                    snapshot_hashes = compute_source_hashes(git_baseline)
                    baseline_origin = "git"
                except Exception:
                    snapshot_hashes = {}
                    baseline_origin = "bootstrap"

        if locales:
            # Trim, drop empties, and dedupe while preserving the order the user typed.
            seen = set()
            target_locales = []
            for raw in locales.split(','):
                loc = raw.strip()
                if loc and loc not in seen:
                    seen.add(loc)
                    target_locales.append(loc)
            missing_files = [
                f"{loc}.json" for loc in target_locales
                if not os.path.isfile(os.path.join(dir, f"{loc}.json"))
            ]
            if missing_files:
                console.print(
                    f"[red]Error: --locales referenced locale files that do not exist in '{dir}':[/red] "
                    f"{', '.join(missing_files)}"
                )
                sys.exit(1)
        else:
            try:
                source_basename = os.path.basename(source)
                target_locales = sorted(
                    f.split('.')[0] for f in os.listdir(dir)
                    if f.endswith('.json')
                    and f != source_basename
                    and f != STATE_FILENAME
                    and not f.startswith('.')
                )
            except OSError as e:
                console.print(f"[red]Error reading directory '{dir}': {e}[/red]")
                sys.exit(1)

        if not target_locales:
            console.print(
                f"[yellow]No target locale files found in '{dir}' to sync.[/yellow] "
                f"[dim]Add files like fr-FR.json next to your source, or pass --locales.[/dim]"
            )
            return

        table = Table(box=None, padding=(0, 2))
        table.add_column("Property", style="bold blue")
        table.add_column("Value", style="white")

        table.add_row("Version", f"[magenta]{__version__}[/magenta]")
        if loaded_path:
            table.add_row("Config", f"[cyan]{loaded_path}[/cyan]")
        table.add_row("Source", f"[green]{source}[/green]")
        table.add_row("Directory", f"[green]{dir}[/green]")
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
        elif dry_run: status_flags.append("[bold yellow]Dry-Run[/bold yellow]")
        if verbose: status_flags.append("[bold cyan]Verbose[/bold cyan]")

        table.add_row("Mode", " + ".join(status_flags) if status_flags else "[dim]Standard[/dim]")

        console.print(Panel(table, title="[bold white]Settings Summary[/bold white]", border_style="blue", expand=False))

        # First-run + no-git tip: surfaces *once* (subsequent runs will have a snapshot).
        if not snapshot_existed and not in_git_repo:
            console.print(
                "[dim]💡 Tip:[/dim] [yellow]This project isn't a git repository.[/yellow] "
                "Initialize one with [cyan]git init[/cyan] so future first-runs can derive a "
                "drift baseline from history."
            )

        results = []
        max_parallel_locales = config_data.get('max_parallel_locales', 3)

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
                futures = [
                    locale_executor.submit(
                        process_locale, locale, source_data, dir, progress, main_task_id, config_data,
                        snapshot_hashes=snapshot_hashes,
                        rewrite=rewrite,
                        prune=prune,
                        update_changed=update_changed,
                        dry_run=dry_run,
                        verbose=verbose,
                    )
                    for locale in target_locales
                ]

                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        crash = LocaleResult("<unknown>")
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

        # Decide which columns to render — drop ones that would be uniformly "—".
        show_missing = total_missing > 0
        show_changed = total_changed > 0
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
        if show_orphans:   summary_table.add_column("Orphans",    justify="right", style="bright_black")
        if show_unchanged: summary_table.add_column("Unchanged",  justify="right", style="dim")
        if show_translated:summary_table.add_column("Translated", justify="right", style="green")
        if show_copied:    summary_table.add_column("Copied",     justify="right", style="magenta")
        if show_pruned:    summary_table.add_column("Pruned",     justify="right", style="bright_magenta")
        if show_failed:    summary_table.add_column("Failed",     justify="right", style="red")

        for r in sorted_results:
            if dry_run:
                pending = r.missing_count + r.changed_count
                if pending > 0:
                    status_text = "[yellow]Pending[/yellow]"
                elif r.orphan_count > 0:
                    status_text = "[bright_black]Orphans only[/bright_black]"
                else:
                    status_text = "[dim]Up to date[/dim]"
            else:
                status_text = {
                    "done": "[green]Done[/green]",
                    "partial": "[yellow]Partial[/yellow]",
                    "failed": "[red]Failed[/red]",
                    "uptodate": "[dim]Up to date[/dim]",
                }[r.status]

            row = [r.locale, status_text]
            def _cell(value, bold=False):
                if not value:
                    return "—"
                return f"[bold]{value}[/bold]" if bold else f"{value}"

            if show_missing:    row.append(_cell(r.missing_count, bold=True))
            if show_changed:    row.append(_cell(r.changed_count, bold=True))
            if show_orphans:    row.append(_cell(r.orphan_count, bold=True))
            if show_unchanged:  row.append(_cell(r.unchanged_count))
            if show_translated: row.append(_cell(r.translated))
            if show_copied:     row.append(_cell(r.copied))
            if show_pruned:     row.append(_cell(r.pruned))
            if show_failed:     row.append(_cell(r.failed))

            summary_table.add_row(*row)

        # If literally nothing happened, skip the table entirely and rely on the footer.
        nothing_to_show = (
            dry_run and total_missing == 0 and total_changed == 0 and total_orphans == 0
        ) or (
            not dry_run and not (
                show_translated or show_copied or show_pruned or show_failed
                or total_missing or total_changed or total_orphans
            )
        )
        if not nothing_to_show:
            console.print(summary_table)

        issues_panel = _render_issues_panel(results)
        if issues_panel:
            console.print()
            console.print(issues_panel)

        # Orphan warning — default behavior leaves them alone, so call it out.
        if total_orphans > 0 and not prune:
            orphan_msg = (
                f"[bold yellow]⚠ {total_orphans} orphan key(s)[/bold yellow] "
                f"(present in target locales but absent from source) were left in place. "
                f"Re-run with [cyan]--prune[/cyan] to remove them."
            )
            console.print()
            console.print(Panel(orphan_msg, border_style="yellow", expand=False))

        # Persist a fresh snapshot, but only advance entries that every locale
        # successfully synced. For keys some locale failed:
        #   - if we already had a previous hash, keep it (next run re-detects drift)
        #   - if it was brand-new, drop it entirely so next run treats it as missing
        if not dry_run:
            try:
                current_hashes = compute_source_hashes(source_data)
                failed_keys = set()
                for r in results:
                    failed_keys.update(r.failed_paths)

                new_hashes = {}
                for key, h in current_hashes.items():
                    if key in failed_keys:
                        if key in snapshot_hashes:
                            new_hashes[key] = snapshot_hashes[key]
                        # else: omit — let next run see it as a missing key.
                    else:
                        new_hashes[key] = h

                save_state(state_path, new_hashes)
            except OSError as e:
                console.print(f"[yellow]⚠ Could not write snapshot {state_path}: {e}[/yellow]")
            except Exception as e:
                console.print(f"[yellow]⚠ Snapshot update skipped: {e}[/yellow]")

        total_time = time.time() - start_time
        console.print()

        if dry_run:
            has_drift = (total_missing + total_changed + total_orphans) > 0
            next_steps = []
            if total_missing > 0 and total_changed == 0 and total_orphans == 0:
                next_steps.append("[cyan]langsync[/cyan] translates the missing keys.")
            elif total_changed > 0 and total_missing == 0:
                next_steps.append("[cyan]langsync --update-changed[/cyan] re-translates only the edited keys.")
            elif total_missing > 0 and total_changed > 0:
                next_steps.append("[cyan]langsync[/cyan] handles missing + changed keys.")
            if total_orphans > 0:
                next_steps.append("[cyan]langsync --prune[/cyan] removes the orphan keys.")
            next_steps_text = ("\n" + "\n".join(f"[dim]→[/dim] {s}" for s in next_steps)) if next_steps else ""

            if check and has_drift:
                headline = "[bold red]✖ Drift detected[/bold red]"
                border_style = "red"
            elif check:
                headline = "[bold green]✓ All locales are in sync[/bold green]"
                border_style = "green"
            elif has_drift:
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
                f"[dim]Orphans:[/dim] [bold bright_black]{total_orphans}[/bold bright_black]"
                f"{next_steps_text}"
            )
        else:
            if total_failed > 0:
                headline = "[bold yellow]⚠ Sync Completed With Issues[/bold yellow]"
                border_style = "yellow"
                tail = f"\n[dim]Re-run langsync to retry the [bold red]{total_failed}[/bold red] failed key(s).[/dim]"
            else:
                headline = "[bold green]✓ Sync Completed Successfully![/bold green]"
                border_style = "green"
                tail = ""

            footer_text = (
                f"{headline}\n"
                f"[dim]Time elapsed:[/dim] [bold cyan]{total_time:.2f}s[/bold cyan]\n"
                f"[dim]Translated:[/dim] [bold green]{total_translated}[/bold green]   "
                f"[dim]Copied:[/dim] [bold magenta]{total_copied}[/bold magenta]   "
                f"[dim]Pruned:[/dim] [bold bright_magenta]{total_pruned}[/bold bright_magenta]   "
                f"[dim]Failed:[/dim] [bold red]{total_failed}[/bold red]"
                f"{tail}"
            )

        console.print(Panel(footer_text, border_style=border_style, expand=False))

        _print_update_banner(poll_update(0.4))

        if check and (total_missing + total_changed + total_orphans) > 0:
            sys.exit(1)

    except KeyboardInterrupt:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()
