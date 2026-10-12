# Contributing to LangSync

Thanks for helping! Small, focused pull requests that keep the CLI simple are the easiest to merge.

## Dev setup

```bash
pipenv install
pipenv run pip install -e .
pipenv run pytest
```

Use `pipenv` for dependencies and for running the tests.

## Ground rules

- **Bump the version for every fix or feature.** `pyproject.toml` is the source of truth, and
  `src/langsync/__init__.py` must match it: `0.1.x` for a fix, `0.x.0` for a feature, `x.0.0` for
  a breaking change.
- **Console output goes through `rich`.** Use `Panel`, `Table` and `Progress`, and escape brackets in
  strings that may contain them (`rf"\[{locale}]"`), or rich reads them as style tags.
- **New commands support `--dry-run` and `--verbose`** where it makes sense.
- **Errors say how to fix them.** Follow the existing `_die(...)` and `_print_fixes(...)` helpers in
  `cli.py`: name the problem, then give the exact command or config change.
- **Never ship a broken ICU message.** Changes to translation or ICU handling keep or extend the tests
  in `tests/test_icu.py` and `tests/test_translator.py`.

## The website

The site in [`site/`](site) is plain HTML, CSS and JS with no build step, deployed to Cloudflare
Pages. `site/_worker.js` serves `install.sh` to `curl` and `wget`, and the static site to browsers.
Preview it with:

```bash
npx wrangler pages dev site
```

The terminal recordings in `docs/media` and `site/media` were made with
[VHS](https://github.com/charmbracelet/vhs).

## Reporting issues

Open an [issue](https://github.com/dracu-lah/langsync-cli/issues/new/choose) with
`langsync --version`, your OS and Python version, and the smallest source string that shows the
problem. Security problems go through [SECURITY.md](SECURITY.md) instead.
