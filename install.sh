#!/bin/sh
# LangSync Install Script
# Can be executed via: curl -sSL https://raw.githubusercontent.com/dracu-lah/langsync-cli/main/install.sh | bash

set -e

echo "Installing LangSync..."

if command -v pipx >/dev/null 2>&1; then
    echo "Using pipx to install globally..."
    pipx install --force git+https://github.com/dracu-lah/langsync-cli.git
elif command -v pip >/dev/null 2>&1; then
    echo "Using pip to install globally..."
    pip install --user --upgrade git+https://github.com/dracu-lah/langsync-cli.git
else
    echo "Error: Neither pipx nor pip is installed. Please install Python and pip first."
    exit 1
fi

echo "LangSync installed successfully!"
echo "Make sure your pip user bin directory is in your PATH."
echo "You can now run 'langsync --help'"
