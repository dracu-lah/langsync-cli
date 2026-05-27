# 🌍 LangSync

**LangSync** is a high-performance, parallel I18N synchronization engine. It keeps your translation files perfectly in sync using a single source file as the "Source of Truth," leveraging batch translation to reduce network overhead by up to 98%.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.6.0-magenta.svg)](pyproject.toml)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

---

## ⚡ Quick Start

Install LangSync globally with a single command:

```bash
curl -sSL langsync.nevil.dev | bash
```

---

## ✨ Key Features

-   **🚀 Parallel Execution:** Syncs multiple locales simultaneously using optimized thread pooling.
-   **📦 Batch Translation:** Groups keys into single requests, drastically reducing translation time and API calls.
-   **🛡️ Smart Protection:** Automatically detects and protects `{variable}` and `<tag>` placeholders.
-   **📝 Whitelist Support:** Keep brand names and technical terms (e.g., "SwayWM", "Lascade") untouched.
-   **📉 Rate Limit Resilience:** Intelligent "Cool Down" mechanism with exponential backoff for API stability.
-   **✨ UI-Aware:** Synchronizes punctuation (like trailing periods) to maintain professional UI consistency.
-   **🔍 Drift Detection:** A `.langsync-state.json` snapshot tracks every source value, so edited keys are re-translated and removed keys can be pruned on demand.
-   **🧹 Opt-in Pruning:** Use `--prune` to drop orphan keys; without it they're surfaced as a warning rather than silently deleted.

---

## 🛠 Usage

Run LangSync in your project root. It automatically detects your configuration.

```bash
# Basic sync (using defaults)
langsync

# Sync specific locales
langsync --locales es-ES,fr-FR

# Force rewrite existing translations
langsync --rewrite
```

---

## ⚙️ Configuration

LangSync searches for configuration in: `langsync.json`, `.langsync.json`, or `~/.langsync.json`.

```json
{
  "source": "messages/en-GB.json",
  "dir": "messages",
  "max_parallel_locales": 5,
  "batch_size": 25,
  "whitelist": ["MyBrand", "ProMode"]
}
```

---

## 🧑‍💻 Development

```bash
# Setup environment
pipenv install
pipenv run pip install -e .

# Run tests
pipenv run pytest
```

---

## 📜 Versioning & Contributions

This project follows **SemVer**.
- **Source of Truth:** `pyproject.toml`
- **Sync:** `src/langsync/__init__.py` must match `pyproject.toml`.

Made with ❤️ for the I18N community.
