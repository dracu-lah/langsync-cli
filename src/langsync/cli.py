import os
import sys
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
from . import __version__

console = Console()


def handle_sigint(signum, frame):
    """Gracefully handle Ctrl+C."""
    console.print("\n[bold red]✖ Interrupted by user. Exiting...[/bold red]")
    sys.exit(0)


class LocaleResult:
    """Outcome of processing a single locale. Holds counts and human-readable issues."""

    __slots__ = ("locale", "translated", "copied", "failed", "missing_count", "issues")

    def __init__(self, locale):
        self.locale = locale
        self.translated = 0
        self.copied = 0
        self.failed = 0
        self.missing_count = 0
        self.issues = []  # list of (kind, message)

    def add_issue(self, kind, message):
        self.issues.append((kind, message))

    @property
    def status(self):
        if self.failed and self.translated == 0 and self.copied == 0:
            return "failed"
        if self.failed:
            return "partial"
        if self.translated or self.copied:
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
            # Retry the ones that came back empty individually.
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
                        result.failed += 1
                        result.add_issue("empty", f"empty response for '{_format_path(path)}'")
                except TranslationError as e:
                    result.failed += 1
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
                result.failed += 1
                result.add_issue("empty", f"empty response for '{_format_path(path)}'")
        except TranslationError as e:
            result.failed += 1
            result.add_issue(e.kind, f"'{_format_path(path)}': {e}")
        finally:
            progress.update(locale_task_id, advance=1)

    return succeeded


def process_locale(locale, source_data, messages_dir, progress, main_task_id, config, rewrite=False, dry_run=False, verbose=False):
    result = LocaleResult(locale)
    target_file = os.path.join(messages_dir, f"{locale}.json")

    try:
        target_data = LocaleProcessor.load_json(target_file)
    except Exception as e:
        result.add_issue("io", f"failed to read {target_file}: {e}")
        progress.update(main_task_id, advance=1)
        return result

    processor = LocaleProcessor(source_data)
    translatable, passthrough = processor.get_missing_keys(target_data, rewrite=rewrite)

    result.missing_count = len(translatable) + len(passthrough)
    result.copied = len(passthrough)

    if dry_run:
        if verbose:
            for path, val in translatable:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [yellow]Pending:[/yellow] [blue]{_format_path(path)}[/blue] -> [italic]{val}[/italic]"
                )
            for path, val in passthrough:
                progress.console.print(
                    rf"[dim]\[{locale}][/dim] [magenta]Copy as-is:[/magenta] [blue]{_format_path(path)}[/blue]"
                )
        progress.update(main_task_id, advance=1)
        return result

    # Copy passthrough (empty / non-string source values) directly — no API calls needed.
    for path, val in passthrough:
        LocaleProcessor.set_value_by_path(target_data, path, val)

    if not translatable:
        try:
            LocaleProcessor.prune_extra_keys(source_data, target_data)
            LocaleProcessor.save_json(target_file, target_data)
        except Exception as e:
            result.add_issue("io", f"failed to write {target_file}: {e}")
        progress.update(main_task_id, advance=1)
        return result

    lang_code = get_translator_code(locale)
    try:
        translator_service = TranslationService(target_lang=lang_code, whitelist=config.get('whitelist'))
    except Exception as e:
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
        LocaleProcessor.prune_extra_keys(source_data, target_data)
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

        # Show up to 2 illustrative messages so the panel stays readable.
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


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('-s', '--source', help='Source JSON file.')
@click.option('-d', '--dir', help='Directory containing locale files.')
@click.option('-l', '--locales', help='Comma-separated list of locales to sync (optional).')
@click.option('-c', '--config', help='Path to config JSON file.')
@click.option('-r', '--rewrite', is_flag=True, help='Rewrite existing keys.')
@click.option('--dry-run', is_flag=True, help='Show what would be translated without making changes.')
@click.option('-v', '--verbose', is_flag=True, help='Enable detailed output during translation.')
@click.version_option(__version__)
def main(source, dir, locales, config, rewrite, dry_run, verbose):
    """Modern I18N sync tool with parallel translation."""
    signal.signal(signal.SIGINT, handle_sigint)

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
                            import json
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
            console.print("[red]Error: No source file configured. Provide it via --source or langsync.json.")
            sys.exit(1)

        if not os.path.exists(source):
            console.print(f"[red]Error: Source file '{source}' not found. Please check your path.")
            sys.exit(1)

        if not dir:
            console.print("[red]Error: No locale directory configured. Provide it via --dir or langsync.json.")
            sys.exit(1)

        if not os.path.exists(dir):
            console.print(f"[red]Error: Directory '{dir}' not found. Please check your path.")
            sys.exit(1)

        try:
            source_data = LocaleProcessor.load_json(source)
        except Exception as e:
            console.print(f"[red]Error reading source JSON file: {e}")
            sys.exit(1)

        if locales:
            target_locales = [l.strip() for l in locales.split(',')]
        else:
            try:
                target_locales = [
                    f.split('.')[0] for f in os.listdir(dir)
                    if f.endswith('.json') and f != os.path.basename(source)
                ]
            except Exception as e:
                console.print(f"[red]Error reading directory '{dir}': {e}")
                sys.exit(1)

        if not target_locales:
            console.print(f"[yellow]Warning: No locale files found in '{dir}' to sync (excluding source).")
            return

        table = Table(box=None, padding=(0, 2))
        table.add_column("Property", style="bold blue")
        table.add_column("Value", style="white")

        table.add_row("Version", f"[magenta]{__version__}[/magenta]")
        if loaded_path:
            table.add_row("Config", f"[cyan]{loaded_path}[/cyan]")
        table.add_row("Source", f"[green]{source}[/green]")
        table.add_row("Directory", f"[green]{dir}[/green]")
        table.add_row("Locales", f"[yellow]{len(target_locales)}[/yellow] ({', '.join(target_locales[:5])}{'...' if len(target_locales) > 5 else ''})")

        status_flags = []
        if rewrite: status_flags.append("[bold red]Rewrite[/bold red]")
        if dry_run: status_flags.append("[bold yellow]Dry-Run[/bold yellow]")
        if verbose: status_flags.append("[bold cyan]Verbose[/bold cyan]")

        table.add_row("Mode", " + ".join(status_flags) if status_flags else "[dim]Standard[/dim]")

        console.print(Panel(table, title="[bold white]Settings Summary[/bold white]", border_style="blue", expand=False))

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
                    locale_executor.submit(process_locale, locale, source_data, dir, progress, main_task_id, config_data, rewrite=rewrite, dry_run=dry_run, verbose=verbose)
                    for locale in target_locales
                ]

                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        # Don't let one locale's crash kill the run.
                        crash = LocaleResult("<unknown>")
                        crash.add_issue("crash", f"locale worker crashed: {e}")
                        results.append(crash)

        summary_title = "\n[bold yellow]Dry Run Statistics[/bold yellow]" if dry_run else "\nSync Statistics"
        summary_table = Table(title=summary_title, box=None, header_style="bold underline white")
        summary_table.add_column("Locale", style="cyan")
        summary_table.add_column("Status")
        if dry_run:
            summary_table.add_column("Missing Keys", justify="right")
        else:
            summary_table.add_column("Translated", justify="right", style="green")
            summary_table.add_column("Copied", justify="right", style="magenta")
            summary_table.add_column("Failed", justify="right", style="red")

        total_translated = 0
        total_copied = 0
        total_failed = 0
        total_missing = 0

        for r in sorted(results, key=lambda x: x.locale):
            if dry_run:
                pending = r.missing_count
                status_text = "[yellow]Pending[/yellow]" if pending > 0 else "[dim]Up to date[/dim]"
                summary_table.add_row(r.locale, status_text, f"[bold]{pending}[/bold]")
                total_missing += pending
            else:
                status = r.status
                status_label = {
                    "done": "[green]Done[/green]",
                    "partial": "[yellow]Partial[/yellow]",
                    "failed": "[red]Failed[/red]",
                    "uptodate": "[dim]Up to date[/dim]",
                }[status]
                summary_table.add_row(
                    r.locale,
                    status_label,
                    f"{r.translated}" if r.translated else "—",
                    f"{r.copied}" if r.copied else "—",
                    f"{r.failed}" if r.failed else "—",
                )
                total_translated += r.translated
                total_copied += r.copied
                total_failed += r.failed

        console.print(summary_table)

        issues_panel = _render_issues_panel(results) if not dry_run else None
        if issues_panel:
            console.print()
            console.print(issues_panel)

        total_time = time.time() - start_time
        console.print()

        if dry_run:
            footer_text = (
                f"[bold yellow]⚠ Dry Run Completed![/bold yellow]\n"
                f"[dim]Time elapsed:[/dim] [bold cyan]{total_time:.2f}s[/bold cyan]\n"
                f"[dim]Total missing keys found:[/dim] [bold magenta]{total_missing}[/bold magenta]\n"
                f"[italic]No changes were made to your files.[/italic]"
            )
            border_style = "yellow"
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
                f"[dim]Failed:[/dim] [bold red]{total_failed}[/bold red]"
                f"{tail}"
            )

        console.print(Panel(footer_text, border_style=border_style, expand=False))

    except KeyboardInterrupt:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()
