# LocSync

LocSync (Locale Sync) is a modern I18N synchronization tool with parallel translation support, built with Python. It's designed to be used as a standalone CLI to keep your translation files (JSON) in sync using a source file as the "Source of Truth."

## Installation

To use `locsync` as a CLI command anywhere, the recommended way on modern Linux (like Arch, Ubuntu 23.04+) and macOS is using `pipx`:

### Using pipx (Recommended)

```bash
# Install pipx if you don't have it (e.g., sudo pacman -S python-pipx)
pipx install .
```

### From Source (Traditional pip)

If your system allows it (or inside a virtual environment):

```bash
git clone https://github.com/youruser/locsync.git
cd locsync
pip install .
```

### Development with Pipenv

```bash
cd locsync
pipenv install
pipenv run locsync
```

## Usage

Once installed, you can simply run:

```bash
locsync
```

**Note:** If no configuration file is found, LocSync uses these defaults:
- **Source:** `messages/en-GB.json`
- **Target Directory:** `messages/`

### CLI Options

- `--source`: Path to the source JSON file (e.g., `messages/en-GB.json`).
- `--dir`: Directory containing other locale files.
- `--locales`: Comma-separated list of locales to sync (optional, e.g., `es-ES,fr-FR`).
- `--config-file`: Path to a custom config JSON file.

## Configuration

LocSync automatically looks for configuration in:
1. `locsync.json` in the current directory.
2. `.locsync.json` in the current directory.
3. `~/.locsync.json` (Global configuration).

### Example `locsync.json`

Create this file in your project root:

```json
{
  "source": "messages/en-GB.json",
  "dir": "messages",
  "max_workers_per_locale": 5,
  "max_parallel_locales": 3,
  "delay_between_requests": 0.2,
  "whitelist": [
    "BrandName",
    "TechnicalTerm"
  ]
}
```

## Features

- **Parallel Processing:** Syncs multiple locales and translates keys in parallel.
- **Whitelist Protection:** Prevents specific terms from being translated.
- **Placeholder Support:** Protects `{variable}` and `<tag>` placeholders.
- **Robust Error Handling:** Clear messages for missing files or invalid configs.
- **Rich Interface:** Beautiful progress bars and summary tables.

## Project Structure

```
locsync/
├── Pipfile
├── Pipfile.lock
├── pyproject.toml
├── README.md
├── locsync.json
└── src/
    └── locsync/
        ├── __init__.py
        ├── __main__.py
        ├── cli.py
        ├── config.py
        ├── processor.py
        └── translator.py
```

## License

MIT
