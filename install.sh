#!/bin/bash
# XTernal for Mac -- one-time installer.
# SPDX-License-Identifier: AGPL-3.0-only
#
# You don't run this yourself -- XTernal.command runs it when needed.
# Safe to run again any time (it just repairs/updates the install).
# Everything it prints is also saved to install.log in this folder.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
exec > >(tee "$HERE/install.log") 2>&1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mINSTALL FAILED:\033[0m %s\n' "$*"
         echo "Send the file install.log (in $HERE) to whoever is helping you."
         exit 1; }

echo "XTernal for Mac installer -- $(date)"
echo "macOS $(sw_vers -productVersion 2>/dev/null) ($(sw_vers -buildVersion 2>/dev/null)), $(uname -m)"
echo "folder: $HERE"

if [ "$(uname)" != "Darwin" ]; then
    fail "this is not a Mac."
fi
case "$HERE" in
    *" "*) echo "(note: the folder path has spaces -- that's fine)";;
esac

say "Unblocking the downloaded files"
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
chmod +x "$HERE/xternal-mac" "$HERE/run.py" "$HERE/XTernal.command" "$HERE/install.sh" 2>/dev/null
echo "ok"

say "Getting a private copy of Python (nothing to click, no password)"
# uv (astral.sh) is one small program that downloads a ready-made Python into
# this folder and installs packages into it. Nothing is installed system-wide:
# delete the folder and it's all gone.
case "${XTERNAL_UV_ASSET:-$(uname -m)}" in
    arm64)   UV_ASSET="aarch64-apple-darwin" ;;
    x86_64)  UV_ASSET="x86_64-apple-darwin" ;;
    *-*)     UV_ASSET="$XTERNAL_UV_ASSET" ;;   # testing override
    *)       fail "unknown Mac processor type: $(uname -m)" ;;
esac
UV="$HERE/.uv/uv"
if ! "$UV" --version >/dev/null 2>&1; then
    rm -rf "$HERE/.uv"
    mkdir -p "$HERE/.uv"
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_ASSET.tar.gz" \
        -o "$HERE/.uv/uv.tar.gz" \
        || fail "could not download the Python tools (check your internet connection)."
    tar -xzf "$HERE/.uv/uv.tar.gz" -C "$HERE/.uv" --strip-components 1 \
        || fail "the Python tools download was damaged. Run XTernal again."
    rm -f "$HERE/.uv/uv.tar.gz"
fi
"$UV" --version || fail "the Python tools won't run on this Mac."

# Keep uv's Python and cache inside this folder, and never use a system Python
# (whatever is or isn't installed on the Mac can't break this).
export UV_PYTHON_INSTALL_DIR="$HERE/.python"
export UV_CACHE_DIR="$HERE/.uv/cache"
export UV_PYTHON_PREFERENCE="only-managed"
export UV_NO_CONFIG=1

say "Creating the private Python environment (.venv)"
if [ -e "$HERE/.venv" ] && ! "$HERE/.venv/bin/python" -c 'import sys' 2>/dev/null; then
    echo "existing .venv is broken -- rebuilding"
    rm -rf "$HERE/.venv"
fi
if [ ! -x "$HERE/.venv/bin/python" ]; then
    "$UV" venv --python 3.12 "$HERE/.venv" \
        || fail "could not set up Python (check your internet connection)."
fi
"$HERE/.venv/bin/python" --version

say "Installing PySide6 + numpy (first time downloads ~100 MB)"
"$UV" pip install --python "$HERE/.venv/bin/python" -r "$HERE/requirements.txt" \
    || fail "could not install the Python packages (check your internet connection)."

say "Checking the install"
"$HERE/.venv/bin/python" -c 'import PySide6.QtWidgets, numpy; print("PySide6", PySide6.__version__, "/ numpy", numpy.__version__)' \
    || fail "packages installed but don't import."

touch "$HERE/.venv/.installed"
echo
echo "Installed."
