#!/bin/sh
# LangSync Install Script
# Can be executed via: curl -sSL https://raw.githubusercontent.com/dracu-lah/langsync-cli/main/install.sh | bash

set -e

echo "Installing LangSync..."

# Detect OS (macOS usually ships with pip3, not pip)
OS="$(uname -s)"

PACKAGE_URL="git+https://github.com/dracu-lah/langsync-cli.git"

# Resolve the correct pip command: prefer pip3 (common on macOS), then pip
if command -v pip3 >/dev/null 2>&1; then
    PIP_CMD="pip3"
elif command -v pip >/dev/null 2>&1; then
    PIP_CMD="pip"
else
    PIP_CMD=""
fi

# Find a stable Python for pipx. pipx + Python 3.14 (brand new) is known to
# fail with 'ensurepip --upgrade --default-pip returned non-zero exit status 1'.
# Prefer 3.13 → 3.12 → 3.11 → 3.10 → 3.9 for pipx's shared venv, and only
# fall back to python3 if none of those exist.
find_stable_python() {
    for v in 3.13 3.12 3.11 3.10 3.9; do
        if command -v "python$v" >/dev/null 2>&1; then
            echo "python$v"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return 0
    fi
    echo ""
    return 1
}

STABLE_PYTHON="$(find_stable_python || true)"

install_pipx_if_needed() {
    if command -v pipx >/dev/null 2>&1; then
        return 0
    fi

    echo "pipx not found. Attempting to install pipx..."

    # macOS: prefer Homebrew if available (avoids PEP 668 "externally-managed-environment")
    if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        echo "Installing pipx via Homebrew..."
        brew install pipx
        pipx ensurepath || true
        return 0
    fi

    # Fallback: install pipx via pip/pip3 with --user
    if [ -n "$PIP_CMD" ]; then
        echo "Installing pipx via $PIP_CMD --user..."
        # --break-system-packages is needed on newer distros / macOS Python 3.12+ (PEP 668)
        "$PIP_CMD" install --user pipx 2>/dev/null || \
            "$PIP_CMD" install --user --break-system-packages pipx
        # Ensure pipx is on PATH for this shell
        if command -v python3 >/dev/null 2>&1; then
            python3 -m pipx ensurepath || true
        fi
        return 0
    fi

    echo "Error: Cannot install pipx — pip/pip3 not found."
    return 1
}

# Remove a broken pipx shared venv (common when pipx initialized against a
# Python version whose ensurepip is broken — e.g. Homebrew python@3.14).
reset_pipx_shared_venv() {
    shared_dir="${PIPX_SHARED_LIBS:-$HOME/.local/pipx/shared}"
    if [ -d "$shared_dir" ]; then
        echo "Removing broken pipx shared venv at $shared_dir ..."
        rm -rf "$shared_dir"
    fi
}

# Try pipx install, and if it fails with the ensurepip / shared-venv error,
# retry with a stable Python version.
pipx_install_with_fallback() {
    # Attempt 1: default pipx behavior (whatever Python it's bound to)
    if pipx install --force "$PACKAGE_URL"; then
        return 0
    fi

    echo ""
    echo "pipx install failed — likely due to a broken shared venv (common with Python 3.14)."
    echo "Retrying with a stable Python interpreter..."

    reset_pipx_shared_venv

    # Attempt 2: explicit --python pointing at a known-good interpreter
    if [ -n "$STABLE_PYTHON" ] && command -v "$STABLE_PYTHON" >/dev/null 2>&1; then
        STABLE_PYTHON_PATH="$(command -v "$STABLE_PYTHON")"
        echo "Using $STABLE_PYTHON_PATH for pipx..."
        if pipx install --force --python "$STABLE_PYTHON_PATH" "$PACKAGE_URL"; then
            return 0
        fi
    fi

    # Attempt 3: on macOS, offer to install a stable Python via Homebrew and retry
    if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        echo "Installing python@3.13 via Homebrew for a stable pipx backend..."
        brew install python@3.13 || true
        reset_pipx_shared_venv
        if command -v python3.13 >/dev/null 2>&1; then
            if pipx install --force --python "$(command -v python3.13)" "$PACKAGE_URL"; then
                return 0
            fi
        fi
    fi

    return 1
}

# Preferred path: install with pipx (isolated, globally-available CLI)
if install_pipx_if_needed && command -v pipx >/dev/null 2>&1; then
    echo "Using pipx to install globally..."
    if pipx_install_with_fallback; then
        :
    else
        echo ""
        echo "pipx install still failed — falling back to $PIP_CMD --user..."
        if [ -n "$PIP_CMD" ]; then
            "$PIP_CMD" install --user --upgrade "$PACKAGE_URL" 2>/dev/null || \
                "$PIP_CMD" install --user --upgrade --break-system-packages "$PACKAGE_URL"
        else
            echo "Error: pipx failed and no pip/pip3 available for fallback."
            exit 1
        fi
    fi
elif [ -n "$PIP_CMD" ]; then
    echo "pipx unavailable — falling back to $PIP_CMD --user..."
    # --break-system-packages handles PEP 668 environments (macOS Python.org, newer Debian/Ubuntu, etc.)
    "$PIP_CMD" install --user --upgrade "$PACKAGE_URL" 2>/dev/null || \
        "$PIP_CMD" install --user --upgrade --break-system-packages "$PACKAGE_URL"
else
    echo "Error: Neither pipx nor pip/pip3 is installed. Please install Python 3 first."
    if [ "$OS" = "Darwin" ]; then
        echo "  On macOS: brew install python  (or install from https://www.python.org/downloads/)"
    fi
    exit 1
fi

echo ""
echo "LangSync installed successfully!"

# Give macOS users a PATH hint — pipx and 'pip --user' bin dirs are often not on PATH by default
if [ "$OS" = "Darwin" ]; then
    echo "If 'langsync' is not found, ensure these are on your PATH:"
    echo "  ~/.local/bin        (pipx / pip --user)"
    echo "  ~/Library/Python/3.x/bin   (macOS system Python --user installs)"
    echo "You can run: pipx ensurepath   (then restart your shell)"
else
    echo "Make sure your pip user bin directory (~/.local/bin) is in your PATH."
fi
echo "You can now run 'langsync --help'"
