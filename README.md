# 🌍 LangSync

**LangSync** (Language Sync) is a high-performance I18N synchronization tool with parallel translation support. It keeps your translation files (JSON) perfectly in sync using a single source file as the "Source of Truth."

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

---

## 🚀 Key Features

-   **Batch Translation:** Translates multiple keys in a single request, reducing network overhead by up to 98%.
-   **Parallel Processing:** Syncs multiple locales in parallel for maximum speed.
-   **Rate Limit Protection:** Intelligent "Cool Down" mechanism and exponential backoff to handle API limits gracefully.
-   **Whitelist Protection:** Prevent specific brand names or technical terms from being translated.
-   **Smart Placeholders:** Automatically protects `{variable}` and `<tag>` placeholders during translation.
-   **Clean UI Translations:** Automatically synchronizes punctuation (like trailing periods) to keep UI labels concise.
-   **Beautiful UI:** Interactive progress bars, versioning, and total execution time reporting.
-   **Pruning:** Automatically removes keys from target files that no longer exist in the source.

---

## 📦 Installation

### 1. Install `pipx` (Recommended)
`pipx` is the best way to install Python CLI tools in isolated environments.

| OS / Distro | Command |
| :--- | :--- |
| **Arch Linux** | `sudo pacman -S python-pipx` |
| **Ubuntu / Debian** | `sudo apt install pipx` |
| **macOS (Homebrew)** | `brew install pipx` |
| **Fedora** | `sudo dnf install pipx` |
| **Windows** | `pip install pipx` |

> **Note:** After installing `pipx`, run `pipx ensurepath` and restart your terminal.

### 2. Install LangSync
Once `pipx` is ready, install LangSync directly from the source:

```bash
# Clone the repository
git clone https://github.com/youruser/langsync.git
cd langsync

# Install as a global CLI tool
pipx install .
```

---

## 🛠 Usage

### Basic Sync
Run LangSync in your project root. By default, it looks for `messages/en-GB.json`.

```bash
langsync
```

### Advanced CLI Options
Customize the sync process with flags:

```bash
# Sync specific locales only
langsync --locales es-ES,fr-FR

# Specify a different source and target directory
langsync --source i18n/main.json --dir i18n/locales/

# Use a custom configuration file
langsync --config-file project-config.json
```

---

## ⚙️ Configuration

LangSync automatically searches for configuration in the following order:
1.  `langsync.json` (Local)
2.  `.langsync.json` (Local Hidden)
3.  `~/.langsync.json` (Global)

### Example `langsync.json`

```json
{
  "source": "messages/en-GB.json",
  "dir": "messages",
  "max_parallel_locales": 8,
  "batch_size": 40,
  "delay_between_requests": 0.4,
  "retry_count": 5,
  "whitelist": [
    "LangSync",
    "SwayWM",
    "Arch Linux"
  ]
}
```

---

## 🧑‍💻 Development Setup

If you want to contribute or run from source without installing globally:

```bash
# Setup environment
pipenv install

# Install in editable mode
pipenv run pip install -e .

# Run the dev version
pipenv run langsync
```

---

## 📂 Project Structure

```text
langsync/
├── pyproject.toml       # Build system & CLI entry points
├── langsync.json        # Configuration (Optional)
└── src/
    └── langsync/        # Main Package
        ├── cli.py       # Command Line Interface
        ├── config.py    # Config & Whitelist Management
        ├── processor.py # JSON Logic & Pruning
        └── translator.py# Translation Engine & Protection
```

---

## 📜 License

**MIT License**

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
