#!/bin/bash
# XTernal for Mac -- the only thing you ever need to run.
# Double-click it (or: bash XTernal.command). First run installs everything,
# then it checks your Mac's settings, then it opens the macro.
cd "$(dirname "$0")" || exit 1
clear
printf '\033[1m=== XTernal for Mac ===\033[0m\n\n'

pause() {
    echo
    read -n 1 -s -r -p "Press any key to close this window." </dev/tty
    echo
}

# 1. Install, or repair/update the install.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ] \
   || ! .venv/bin/python -c 'import PySide6.QtWidgets, numpy' 2>/dev/null; then
    echo "First-time setup -- this takes a few minutes. Don't close this window."
    if ! bash install.sh; then
        pause
        exit 1
    fi
fi

# 2. SIP + permissions, with step-by-step help if anything is missing.
if ! .venv/bin/python run.py preflight; then
    pause
    exit 1
fi

# 3. Run the macro (asks for the Mac password).
echo
echo "Starting... Type your Mac password when asked."
echo "(Nothing shows while you type it. That's normal. Press Enter after.)"
echo
XTERNAL_QUIET_SUDO=1 ./xternal-mac
code=$?

# 4. If it crashed, make the support report automatically.
if [ $code -ne 0 ] && [ $code -ne 130 ]; then
    echo
    printf '\033[31mSomething went wrong. Making a report for you...\033[0m\n'
    ./xternal-mac report
    echo
    echo "Send the 'xternal-report' zip file on your Desktop to whoever helps you."
fi
pause
