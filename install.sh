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

say "Looking for Python 3.10 or newer"
PY=""
CANDIDATES=""
for v in 3.13 3.12 3.11 3.10 3.14; do
    CANDIDATES="$CANDIDATES /Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
    CANDIDATES="$CANDIDATES /opt/homebrew/bin/python$v /usr/local/bin/python$v"
done
CANDIDATES="$CANDIDATES /opt/homebrew/bin/python3 /usr/local/bin/python3"
# Apple's /usr/bin/python3 is only real once the Command Line Tools exist;
# otherwise running it pops an install dialog, so only try it in that case.
if xcode-select -p >/dev/null 2>&1; then
    CANDIDATES="$CANDIDATES /usr/bin/python3"
fi
for p in $CANDIDATES; do
    [ -x "$p" ] || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$p"
        break
    fi
    echo "  skipping $p ($("$p" --version 2>&1)) -- too old"
done

if [ -z "$PY" ]; then
    echo
    echo "No suitable Python found. Install it (one time, ~2 minutes):"
    echo "  1. A web page is opening: python.org's Mac downloads."
    echo "  2. Download the latest 'macOS 64-bit universal2 installer' for Python 3.13."
    echo "  3. Open the downloaded .pkg and click Continue/Install until it's done."
    echo "  4. Run XTernal again."
    open "https://www.python.org/downloads/macos/" 2>/dev/null
    fail "Python 3.10+ is not installed yet (see steps above)."
fi
echo "using $PY ($("$PY" --version 2>&1))"

say "Creating the private Python environment (.venv)"
if [ -x "$HERE/.venv/bin/python" ] && ! "$HERE/.venv/bin/python" -c 'import sys' 2>/dev/null; then
    echo "existing .venv is broken -- rebuilding"
    rm -rf "$HERE/.venv"
fi
if [ ! -x "$HERE/.venv/bin/python" ]; then
    "$PY" -m venv "$HERE/.venv" || fail "could not create the Python environment."
fi
echo "ok"

say "Installing PySide6 + numpy (first time downloads ~100 MB)"
"$HERE/.venv/bin/python" -m pip install --upgrade pip --disable-pip-version-check -q \
    || echo "(pip self-update failed; continuing)"
"$HERE/.venv/bin/python" -m pip install --disable-pip-version-check -r "$HERE/requirements.txt" \
    || fail "could not install the Python packages (check your internet connection)."

say "Checking the install"
"$HERE/.venv/bin/python" -c 'import PySide6.QtWidgets, numpy; print("PySide6", PySide6.__version__, "/ numpy", numpy.__version__)' \
    || fail "packages installed but don't import."

touch "$HERE/.venv/.installed"
echo
echo "Installed."
