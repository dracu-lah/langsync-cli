# 🌍 LangSync

**LangSync** is a high-performance, parallel I18N synchronization engine. It keeps your translation files perfectly in sync using a single source file as the "Source of Truth," leveraging batch translation to reduce network overhead by up to 98%.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.7.0-magenta.svg)](pyproject.toml)
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
-   **🔢 ICU MessageFormat Safe:** Messages with `plural` / `selectordinal` / `select` arguments are parsed into an AST; only the human-readable branch text is sent for translation, while argument names, branch keywords (`one`, `other`, `=1`, …) and the `#` placeholder are structurally locked. Every rebuilt message is re-validated with the parser before it is written — a translation that would break ICU syntax is rejected (and retried on the next run) instead of shipped.
-   **🩹 Corpus Validation & Self-Healing:** Every run parse-validates existing target strings against their source. A target that is broken ICU (e.g. a machine-translated `other` keyword from an older tool) is flagged as **Broken ICU** and automatically re-translated in place — even if the drift snapshot says it's unchanged. `--check` fails CI on broken strings, and source strings that look like ICU but don't parse are refused (and reported) rather than corrupted.
-   **🧩 Deep Structure Support:** Recurses into arrays and nested objects — strings inside a list (FAQs, feature cards, bullet lists) are translated element by element, not copied verbatim. Drift is tracked per array element (`items[0].title`).
-   **📝 Whitelist Support:** Keep brand names and technical terms (e.g., "SwayWM", "Lascade") untouched.
-   **📉 Rate Limit Resilience:** Intelligent "Cool Down" mechanism with exponential backoff for API stability.
-   **✨ UI-Aware:** Synchronizes punctuation (like trailing periods) to maintain professional UI consistency.
-   **🔍 Drift Detection:** A `.langsync-state.json` snapshot tracks every source value, so edited keys are re-translated and removed keys can be pruned on demand.
-   **🧹 Opt-in Pruning:** Use `--prune` to drop orphan keys; without it they're surfaced as a warning rather than silently deleted.
-   **💡 Actionable Errors:** Every error and warning is followed by a **How to fix** panel with the exact command to run — a missing locale dir prints the `mkdir` that creates it, a rate-limited run prints the config values to slow it down, failed keys print a `langsync --locales …` scoped to just the affected locales, and invalid ICU source strings print the before/after edit they need.

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
