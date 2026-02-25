# Gemini CLI - Project Specific Instructions

## Dependency Management Rule
-   **CRITICAL:** You **MUST** use `pipenv` for all Python dependency management and test execution (e.g., `pipenv install`, `pipenv run pytest`).

## Versioning Rule
-   **CRITICAL:** Every time you implement a new feature or fix a bug, you **MUST** bump the version in `pyproject.toml`.
-   **Source of Truth:** `pyproject.toml` is the source of truth for the version.
-   **Synchronization:** Immediately after updating `pyproject.toml`, you **MUST** update `src/langsync/__init__.py` to match the new version.
-   **Logic:**
    -   Use `0.1.X` for patches/fixes.
    -   Use `0.X.0` for new features.
    -   Use `X.0.0` for major releases/breaking changes.

## UI/UX Standards
-   **Rich Integration:** Maintain the `rich` library for all console output. Use `Panel`, `Table`, and `Progress` to keep the UI modern and visually organized.
-   **Bracket Escaping:** When using Rich to print strings that might contain brackets (like `[locale]`), always use raw strings with escaped brackets (e.g., `rf"\[{locale}]"`) to prevent Rich from misinterpreting them as style tags.
-   **Standard Flags:** Ensure all new CLI commands or major updates support `--dry-run` and `--verbose` where applicable to provide a consistent user experience.
