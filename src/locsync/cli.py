import os
import sys
import click
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.live import Live
from rich.panel import Panel

from .translator import TranslationService, get_translator_code
from .processor import LocaleProcessor
from .config import load_config

console = Console()

def process_locale(locale, source_data, messages_dir, progress, main_task_id, config):
    target_file = os.path.join(messages_dir, f"{locale}.json")
    target_data = LocaleProcessor.load_json(target_file)
    
    processor = LocaleProcessor(source_data)
    missing_items = processor.get_missing_keys(target_data)
    
    if not missing_items:
        progress.update(main_task_id, advance=1)
        # Still prune extra keys
        LocaleProcessor.prune_extra_keys(source_data, target_data)
        LocaleProcessor.save_json(target_file, target_data)
        return locale, 0

    lang_code = get_translator_code(locale)
    translator_service = TranslationService(target_lang=lang_code, whitelist=config.get('whitelist'))
    
    locale_task_id = progress.add_task(f"[cyan]{locale}", total=len(missing_items))
    
    translated_count = 0
    
    max_workers_per_locale = config.get('max_workers_per_locale', 5)
    delay = config.get('delay_between_requests', 0.2)
    
    with ThreadPoolExecutor(max_workers=max_workers_per_locale) as executor:
        future_to_item = {
            executor.submit(translator_service.translate, value, delay): (path, value)
            for path, value in missing_items
        }
        
        for future in as_completed(future_to_item):
            path, original_value = future_to_item[future]
            try:
                translated_value = future.result()
                LocaleProcessor.set_value_by_path(target_data, path, translated_value)
                translated_count += 1
            except Exception as e:
                console.print(f"[red]Error translating {locale} path {'.'.join(path)}: {e}")
            
            progress.update(locale_task_id, advance=1)

    LocaleProcessor.prune_extra_keys(source_data, target_data)
    LocaleProcessor.save_json(target_file, target_data)
    
    progress.remove_task(locale_task_id)
    progress.update(main_task_id, advance=1)
    
    return locale, translated_count

@click.command()
@click.option('--source', help='Source JSON file.')
@click.option('--dir', help='Directory containing locale files.')
@click.option('--locales', help='Comma-separated list of locales to sync (optional).')
@click.option('--config-file', help='Path to config JSON file.')
def main(source, dir, locales, config_file):
    """Modern I18N sync tool with parallel translation."""
    
    # Load configuration
    config, loaded_path = load_config(config_file)
    
    # Override config with CLI options if provided
    source = source or config.get('source')
    dir = dir or config.get('dir')

    # Enhanced Error Handling
    if not source:
        console.print("[red]Error: No source file configured. Provide it via --source or locsync.json.")
        sys.exit(1)
    
    if not os.path.exists(source):
        console.print(f"[red]Error: Source file '{source}' not found. Please check your path.")
        sys.exit(1)

    if not dir:
        console.print("[red]Error: No locale directory configured. Provide it via --dir or locsync.json.")
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

    config_msg = f"Source: [green]{source}[/green]\nLocales to sync: [yellow]{len(target_locales)}[/yellow]"
    if loaded_path:
        config_msg = f"Using config from: [cyan]{loaded_path}[/cyan]\n" + config_msg

    console.print(Panel.fit(
        f"[bold blue]LocSync Tool[/bold blue]\n" + config_msg,
        title="Configuration"
    ))

    results = []
    
    max_parallel_locales = config.get('max_parallel_locales', 3)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        main_task_id = progress.add_task("[bold green]Total Progress", total=len(target_locales))
        
        with ThreadPoolExecutor(max_workers=max_parallel_locales) as locale_executor:
            futures = [
                locale_executor.submit(process_locale, locale, source_data, dir, progress, main_task_id, config)
                for locale in target_locales
            ]
            
            for future in as_completed(futures):
                results.append(future.result())

    # Summary table
    table = Table(title="Sync Summary")
    table.add_column("Locale", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Translated Keys", justify="right")

    for locale, count in sorted(results):
        status = "[green]Done[/green]" if count > 0 else "[white]Up to date[/white]"
        table.add_row(locale, status, str(count))

    console.print(table)

if __name__ == "__main__":
    main()
