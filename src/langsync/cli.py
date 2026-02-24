import os
import sys
import click
import time
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.json import JSON

import threading
from .translator import TranslationService, get_translator_code
from .processor import LocaleProcessor
from .config import load_config, GLOBAL_CONFIG_PATH, LOCAL_CONFIG_NAMES, get_default_config, save_config
from . import __version__

console = Console()

BANNER = """
[bold cyan]
   __                                           
  / /  ___ _  ___  ___ _  ___  __ __  ___  ____ 
 / /__/ _ `/ / _ \/ _ `/ (_-< / // / / _ \/ __/ 
/____/\_,_/ /_//_/\_, / /___/ \_, / /_//_/\__/  
                 /___/       /___/              
[/bold cyan]
[dim cyan]Modern I18N Synchronization Tool • v{}[/dim cyan]
""".format(__version__)

def handle_sigint(signum, frame):
    """Gracefully handle Ctrl+C."""
    console.print("\n[bold red]✖ Interrupted by user. Exiting...[/bold red]")
    sys.exit(0)

def start_key_listener():
    """Listens for Esc or Ctrl+X to exit during processing."""
    # Set up signal handler for Ctrl+C
    signal.signal(signal.SIGINT, handle_sigint)

    def _listener():
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    char = sys.stdin.read(1)
                    if char in ['\x1b', '\x18']:  # Esc, Ctrl+X
                        os._exit(0)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

    thread = threading.Thread(target=_listener, daemon=True)
    thread.start()

def process_locale(locale, source_data, messages_dir, progress, main_task_id, config, rewrite=False):
    target_file = os.path.join(messages_dir, f"{locale}.json")
    target_data = LocaleProcessor.load_json(target_file)
    
    processor = LocaleProcessor(source_data)
    missing_items = processor.get_missing_keys(target_data, rewrite=rewrite)
    
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
    batch_size = config.get('batch_size', 25)
    delay = config.get('delay_between_requests', 0.2)
    retry_count = config.get('retry_count', 3)
    
    # Split missing items into batches
    batches = [missing_items[i:i + batch_size] for i in range(0, len(missing_items), batch_size)]
    
    for batch in batches:
        paths = [item[0] for item in batch]
        values = [item[1] for item in batch]
        
        translated_values = None
        for attempt in range(retry_count):
            try:
                translated_values = translator_service.translate_batch(values, delay)
                if translated_values and len(translated_values) == len(values):
                    break
            except Exception as e:
                if "RATE_LIMIT_HIT" in str(e):
                    # Pause and increase delay for this locale
                    cooldown = 2 * (attempt + 1)
                    console.print(f"[yellow][Rate Limit] {locale} cooling down for {cooldown}s...[/yellow]")
                    time.sleep(cooldown)
                    delay = min(delay * 2, 2.0) # Gradually increase delay up to 2s
                else:
                    if attempt == retry_count - 1:
                        console.print(f"[red]Error translating {locale} batch after {retry_count} attempts: {e}")
                    time.sleep(1 * (attempt + 1)) # Exponential backoff
        
        if translated_values and len(translated_values) == len(values):
            for path, trans_val in zip(paths, translated_values):
                LocaleProcessor.set_value_by_path(target_data, path, trans_val)
                translated_count += 1
                progress.update(locale_task_id, advance=1)
        else:
            # If batch failed, skip this batch to avoid blocking
            progress.update(locale_task_id, advance=len(values))

    LocaleProcessor.prune_extra_keys(source_data, target_data)
    LocaleProcessor.save_json(target_file, target_data)
    
    progress.remove_task(locale_task_id)
    progress.update(main_task_id, advance=1)
    
    return locale, translated_count

@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.option('-s', '--source', help='Source JSON file.')
@click.option('-d', '--dir', help='Directory containing locale files.')
@click.option('-l', '--locales', help='Comma-separated list of locales to sync (optional).')
@click.option('-c', '--config', help='Path to config JSON file.')
@click.option('-r', '--rewrite', is_flag=True, help='Rewrite existing keys.')
def main(source, dir, locales, config, rewrite):
    """Modern I18N sync tool with parallel translation."""
    try:
        console.print(BANNER)
        start_time = time.time()
        
        # Load configuration
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
        
        # Override config with CLI options if provided
        source = source or config_data.get('source')
        dir = dir or config_data.get('dir')
        rewrite = rewrite or config_data.get('rewrite', False)

        # Enhanced Error Handling
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

        # Settings Summary
        table = Table(box=None, padding=(0, 2))
        table.add_column("Property", style="bold blue")
        table.add_column("Value", style="white")

        table.add_row("Version", f"[magenta]{__version__}[/magenta]")
        if loaded_path:
            table.add_row("Config", f"[cyan]{loaded_path}[/cyan]")
        table.add_row("Source", f"[green]{source}[/green]")
        table.add_row("Directory", f"[green]{dir}[/green]")
        table.add_row("Locales", f"[yellow]{len(target_locales)}[/yellow] ({', '.join(target_locales[:5])}{'...' if len(target_locales) > 5 else ''})")
        table.add_row("Rewrite Mode", "[bold red]Enabled[/bold red]" if rewrite else "[dim]Disabled[/dim]")

        console.print(Panel(table, title="[bold white]Settings Summary[/bold white]", border_style="blue", expand=False))

        # Start key listener now, after all initial input
        start_key_listener()
        
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
                    locale_executor.submit(process_locale, locale, source_data, dir, progress, main_task_id, config_data, rewrite=rewrite)
                    for locale in target_locales
                ]
                
                for future in as_completed(futures):
                    results.append(future.result())

        # Summary table
        summary_table = Table(title="\nSync Statistics", box=None, header_style="bold underline white")
        summary_table.add_column("Locale", style="cyan")
        summary_table.add_column("Status", style="green")
        summary_table.add_column("Translated", justify="right")

        total_translated = 0
        for locale, count in sorted(results):
            status = "[green]Done[/green]" if count > 0 else "[dim white]Up to date[/dim white]"
            summary_table.add_row(locale, status, f"[bold]{count}[/bold]")
            total_translated += count

        console.print(summary_table)
        
        total_time = time.time() - start_time
        console.print("\n" + Panel(
            f"[bold green]✓ Sync Completed Successfully![/bold green]\n"
            f"[dim]Time elapsed:[/dim] [bold cyan]{total_time:.2f}s[/bold cyan]\n"
            f"[dim]Total translated keys:[/dim] [bold magenta]{total_translated}[/bold magenta]",
            border_style="green",
            expand=False
        ))
    
    except KeyboardInterrupt:
        handle_sigint(None, None)

if __name__ == "__main__":
    main()
