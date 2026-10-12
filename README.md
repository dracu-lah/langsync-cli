<p align="center">
  <img src="site/favicon.svg" width="72" alt="LangSync logo" />
</p>

<h1 align="center">LangSync</h1>

<p align="center">One source file, every language in sync. A CLI that fills missing translation keys,<br>re-translates edited ones and keeps ICU plurals valid, for JSON, ARB and YAML i18n projects.</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="pyproject.toml"><img alt="Version" src="https://img.shields.io/badge/version-0.11.0-89dceb" /></a>
  <img alt="Python 3.7+" src="https://img.shields.io/badge/python-3.7%2B-3776ab?logo=python&logoColor=white" />
  <a href="https://github.com/dracu-lah/langsync-cli/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/dracu-lah/langsync-cli/actions/workflows/ci.yml/badge.svg" /></a>
</p>

<p align="center">
  <a href="https://langsync.pages.dev"><img alt="Website" src="https://img.shields.io/badge/site-langsync.pages.dev-1e1e2e?logo=cloudflarepages&logoColor=white" /></a>
  <a href="https://github.com/sponsors/dracu-lah"><img alt="Sponsor on GitHub" src="https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?logo=githubsponsors&logoColor=white" /></a>
  <a href="https://buymeacoffee.com/nevil.dev"><img alt="Buy me a coffee" src="https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?logo=buymeacoffee&logoColor=black" /></a>
</p>

<p align="center">
  <img src="docs/media/demo.gif" width="90%" alt="langsync syncing three locales from one English file, then jq showing the French and German plurals intact, then langsync --check catching drift" />
</p>

## Install

```bash
curl -sSL langsync.nevil.dev | bash
```

The script installs with [pipx](https://pipx.pypa.io) and falls back to `pip --user`. Run the same
line again to update. To do it by hand:

```bash
pipx install git+https://github.com/dracu-lah/langsync-cli.git
```

## Quick start

Run `langsync` in your project. With no config yet, it finds your locale files (`messages/`,
`locales/`, `public/locales/`, `src/i18n/`, `lib/l10n/` and the like) and offers to write a
`langsync.json` for you. Or write one by hand:

```json
{ "source": "messages/en.json", "dir": "messages" }
```

```bash
langsync --add fr,de,ja
```

Every key in `en.json` now exists in `fr.json`, `de.json` and `ja.json`, translated. Run `langsync`
again after editing the source and only the new and changed keys go out.

## What it does

- **Only what changed.** A snapshot of every source string tells new keys from edited ones.
  Translations you did not touch stay as they are.
- **ICU plurals stay valid.** Only the words inside `plural`, `select` and `selectordinal` branches
  are translated. Each rebuilt message is parsed before it is written, and a broken one is rejected.
- **Repairs broken strings.** A target with broken ICU, often left by an older tool, is re-translated
  in place on the next run.
- **Batched and parallel.** Keys go out in batches and locales run side by side, with backoff when
  the API rate limits.
- **Placeholders untouched.** `{name}`, `{{name}}`, `%s`, `%{name}`, `:name`, `$t(key)`, `<b>` and
  `<0>` tags, URLs and whitelisted brand names come back exactly as they went in. A translation
  that drops one is rejected, not written. Arrays and nested objects go element by element.
- **Placeholder checks.** Existing translations whose variables or tags no longer match the source
  are reported, so a renamed `{nmae}` doesn't ship.
- **Your files stay yours.** Indentation, line endings, BOM and key order are kept, unchanged files
  are not rewritten, and every write is atomic. `"key_order": "source"` mirrors the source order.
- **Any layout.** One file per locale (`messages/fr.json`), one folder per locale with namespaces
  (`public/locales/fr/common.json`), Flutter ARB (`app_fr.arb`) or YAML.
- **Errors with the fix.** Every error and warning ends with the command that fixes it, ready to
  paste.

<p align="center">
  <img src="docs/media/plurals.png" width="45%" alt="The French and German cart strings with their ICU plural structure unchanged" />
  <img src="docs/media/fix.png" width="45%" alt="Two errors, each followed by a How to fix box with the exact command to run" />
</p>

## Usage

```bash
langsync                     # sync every locale file in "dir"
langsync --dry-run           # show what would change, write nothing
langsync --add ja,pt-BR      # create these locales and translate them
langsync -l fr-FR,de-DE      # only these locales
langsync --update-changed    # re-translate edited keys only, skip missing ones
langsync --rewrite           # re-translate everything (replaces existing translations)
langsync --prune             # remove keys the source no longer has
langsync --check             # exit 1 on drift, for CI
langsync --stats             # coverage per locale
langsync --json              # machine-readable report on stdout
langsync -p deepl            # use another provider for this run
langsync -v                  # print every key as it is processed
```

A sync that leaves keys untranslated exits 1, so scripts notice. Re-running picks those keys up;
each locale tracks its own progress, so syncing only `-l fr` never hides an edit from `de`.

### In CI

`--check` writes nothing and exits 1 when any locale has missing, changed or orphan keys, so a pull
request that edits `en.json` without syncing fails:

```yaml
- run: pipx install git+https://github.com/dracu-lah/langsync-cli.git
- run: langsync --check
```

<p align="center">
  <img src="docs/media/check.png" width="70%" alt="langsync --check reporting one missing and one changed key per locale" />
</p>

## Configuration

LangSync reads `langsync.json` or `.langsync.json` from the current directory or the nearest parent
(up to the git root), then `~/.langsync.json`. Paths in it are relative to the file. Flags override it.

```json
{
  "source": "messages/en-GB.json",
  "dir": "messages",
  "locales": ["fr", "de", "pt-BR"],
  "whitelist": ["MyBrand", "ProMode"],
  "glossary": { "fr": { "Dashboard": "Tableau de bord" } },
  "exclude": ["meta.*", "*.url"],
  "key_order": "source"
}
```

| Key | What it does |
| --- | --- |
| `source`, `dir` | The source file and the folder of `<locale>.json` files (`.arb`, `.yaml` work too). |
| `pattern`, `source_locale` | Instead of `source`/`dir`: a path with `{locale}` and optionally `{ns}`, e.g. `"public/locales/{locale}/{ns}.json"` with `"source_locale": "en"`. |
| `locales` | Locales that should exist. Missing files are created on the next run. |
| `whitelist` | Terms that are never translated (case-sensitive). |
| `glossary` | Forced translations per language or locale. |
| `exclude` | Glob patterns of keys that are copied as-is, never translated. |
| `key_order` | `keep` (default), `source` or `alpha`. |
| `provider` | `google` (default, no key), `deepl`, `libre`, `microsoft` or `mymemory`. |
| `api_key_env`, `provider_url` | Env var holding the key (defaults: `DEEPL_API_KEY`, `LIBRETRANSLATE_API_KEY`, `MICROSOFT_TRANSLATOR_KEY`), and the LibreTranslate server URL. |
| `source_lang`, `language_map` | Override the source language, or map a locale to the provider's code (`{"nb-NO": "no"}`). |
| `batch_size`, `max_parallel_locales`, `max_workers_per_locale`, `delay_between_requests`, `retry_count` | Throughput and rate-limit tuning. |

The source language comes from the source file name (`en-GB.json` is English); name it something else
and set `source_lang`. YAML files need PyYAML: `pipx inject langsync pyyaml`.

The snapshot lives in `<dir>/.langsync-state.json` (for a `pattern`, in the folder above the locale
folders). Commit it, so the next person's run knows which keys you already translated. On a first run
inside a git repository, LangSync builds it from history.

## Development

```bash
pipenv install
pipenv run pip install -e .
pipenv run pytest
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the ground rules. The website lives in [site/](site) and
deploys to Cloudflare Pages.

## Support

- **Bugs and ideas:** [Issues](https://github.com/dracu-lah/langsync-cli/issues). Security reports
  follow [SECURITY.md](SECURITY.md).
- **Chip in:** LangSync is free and MIT licensed. If it saves you time,
  [sponsor it on GitHub](https://github.com/sponsors/dracu-lah) or
  [buy me a coffee](https://buymeacoffee.com/nevil.dev). A star helps the next person find it.

## License

[MIT](LICENSE) © dracu-lah. Translations go through Google Translate (or the provider you pick) via
[deep-translator](https://github.com/nidhaloff/deep-translator).
