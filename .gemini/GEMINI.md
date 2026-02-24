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
-   **Current Task:** Since you just added several features (config discovery, sigint handling, etc.), you should bump the version now.
