#!/bin/bash
# Install the pinned project environment and macOS media dependencies.

set -euo pipefail

command -v brew >/dev/null || {
    echo "Homebrew is required: https://brew.sh"
    exit 1
}

brew install ffmpeg cairo pango sox git-lfs
git lfs install --local

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

if [ -f requirements.lock ]; then
    .venv/bin/python -m pip install --requirement requirements.lock
else
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install --requirement requirements.txt
    lock_tmp="$(mktemp "${TMPDIR:-/tmp}/chalk-requirements.XXXXXX")"
    .venv/bin/python -m pip freeze --all > "$lock_tmp"
    mv "$lock_tmp" requirements.lock
    echo "wrote requirements.lock — commit it with the project"
fi

if ! command -v latex >/dev/null || ! command -v dvisvgm >/dev/null; then
    echo "warning: MathTex requires a LaTeX distribution with dvisvgm"
fi

echo "ready — run ./chalk"
