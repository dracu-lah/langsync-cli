<p align="center">
  <img src="site/favicon.svg" width="72" alt="LangSync logo" />
</p>

<h1 align="center">LangSync</h1>

<p align="center">One source file, every language in sync. A CLI that fills missing translation keys,<br>re-translates edited ones and keeps ICU plurals valid, for any JSON i18n project.</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="pyproject.toml"><img alt="Version" src="https://img.shields.io/badge/version-0.10.0-89dceb" /></a>
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

Add a `langsync.json` to your project root, plus an empty `{}` file for each language you want:

```json
{ "source": "messages/en.json", "dir": "messages" }
```

```bash
echo '{}' > messages/fr.json
langsync
```

Every key in `en.json` now exists in `fr.json`, translated. Run it again after editing the source and
only the new and changed keys go out.

## What it does

- **Only what changed.** A snapshot of every source string tells new keys from edited ones.
  Translations you did not touch stay as they are.
- **ICU plurals stay valid.** Only the words inside `plural`, `select` and `selectordinal` branches
  are translated. Each rebuilt message is parsed before it is written, and a broken one is rejected.
- **Repairs broken strings.** A target with broken ICU, often left by an older tool, is re-translated
  in place on the next run.
- **Batched and parallel.** Keys go out in batches and locales run side by side, with backoff when
  the API rate limits.
- **Placeholders untouched.** `{name}`, `<b>tags</b>` and whitelisted brand names come back exactly
  as they went in. Arrays and nested objects are translated element by element.
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
langsync -l fr-FR,de-DE      # only these locales
langsync --update-changed    # re-translate edited keys only, skip missing ones
langsync --rewrite           # re-translate everything (replaces existing translations)
langsync --prune             # remove keys the source no longer has
langsync --check             # exit 1 on drift, for CI
langsync -v                  # print every key as it is processed
```

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

LangSync reads the first of `langsync.json`, `.langsync.json` or `~/.langsync.json`. Flags override it.

```json
{
  "source": "messages/en-GB.json",
  "dir": "messages",
  "max_parallel_locales": 5,
  "batch_size": 25,
  "whitelist": ["MyBrand", "ProMode"]
}
```

The snapshot lives in `<dir>/.langsync-state.json`. Commit it, so the next person's run knows which
keys you already translated. On a first run inside a git repository, LangSync builds it from history.

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

[MIT](LICENSE) © dracu-lah. Translations go through Google Translate via
[deep-translator](https://github.com/nidhaloff/deep-translator).
