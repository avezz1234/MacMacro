"""
XTernal for Mac -- the hand-holding before launch.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Runs as the normal user, before the password prompt. Each step either passes
silently or stops, says exactly what to click, opens the right Settings page,
and waits until it's done -- so XTernal.command can be the only thing anyone
ever has to run.

Exit code 0 = ready to launch, anything else = stop (the message says why).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from .log import get_logger
from .macos import native

log = get_logger()

PANE_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANE_INPUT_MONITORING = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOLD, RED, GREEN, YELLOW, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"


def _box(title: str, lines: list[str], color: str = YELLOW) -> None:
    print()
    print(f"{color}{BOLD}{'=' * 64}{OFF}")
    print(f"{color}{BOLD}  {title}{OFF}")
    print(f"{color}{BOLD}{'=' * 64}{OFF}")
    for line in lines:
        print("  " + line)
    print()


def _open(url: str) -> None:
    try:
        subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _fresh_check(expr: str) -> bool:
    """Ask a brand-new process, so a permission granted a second ago is seen."""
    code = (
        f"import sys; sys.path.insert(0, {ROOT!r}); "
        f"from xternal.macos import native as n; print(bool({expr}))"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "True"


def _wait_for(expr: str, seconds: int, what: str) -> bool:
    print("  Waiting for you to switch it on", end="", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _fresh_check(expr):
            print(f"\n  {GREEN}{what}: done!{OFF}")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print()
    return False


ACCESS_EXPR = "n.accessibility_trusted() or n.post_event_access()"
LISTEN_EXPR = "n.listen_event_access() is not False"


def run() -> int:
    from .memory.proc import sip_allows_debugging, sip_status_text

    if sys.platform != "darwin":
        print("This only runs on a Mac.")
        return 1
    if not native.available():
        print(f"Can't load macOS system libraries: {native.load_error()}")
        log.error("preflight: native unavailable: %s", native.load_error())
        return 1

    # -- 1. SIP -----------------------------------------------------------------
    sip = sip_allows_debugging()
    log.info("preflight: SIP allows debugging = %s (%s)", sip,
             " ".join(sip_status_text().split()))
    if sip is False:
        apple_silicon = os.uname().machine == "arm64"
        steps = [
            "Roblox's memory is locked by a Mac security setting (SIP).",
            "You have to unlock it ONCE. It takes about 5 minutes:",
            "",
            "  1. Apple menu (top-left) > Shut Down. Wait until the screen is black.",
        ]
        if apple_silicon:
            steps += [
                "  2. Press and HOLD the power button. Keep holding until you see",
                "     'Loading startup options'. Then let go.",
                "  3. Click 'Options', then 'Continue'. Type your password if asked.",
            ]
        else:
            steps += [
                "  2. Press the power button, then immediately hold Command + R",
                "     until you see the Apple logo. Then let go.",
                "  3. Type your password if asked.",
            ]
        steps += [
            "  4. At the top of the screen click 'Utilities', then 'Terminal'.",
            "  5. Type this exactly, then press Enter:",
            "",
            f"        {BOLD}csrutil enable --without debug{OFF}",
            "",
            "     If it asks a question, type  y  and press Enter.",
            "  6. Apple menu > Restart.",
            "  7. When the Mac is back on, run XTernal again.",
            "",
            "(Take a photo of these steps with your phone -- this window",
            " won't be visible while you do them.)",
        ]
        _box("ONE-TIME STEP: unlock Roblox's memory", steps, RED)
        return 2

    # -- 2. Accessibility (clicks + keys) --------------------------------------
    access = native.accessibility_trusted() or native.post_event_access()
    log.info("preflight: accessibility/post-events = %s", access)
    if not access:
        native.request_permissions()
        _open(PANE_ACCESSIBILITY)
        _box("ONE-TIME STEP: let the macro click for you", [
            "A Settings window just opened on 'Accessibility'.",
            "",
            "  1. Find 'Terminal' in the list and switch it ON (blue).",
            "     Not in the list? Click the + button, open 'Applications',",
            "     then 'Utilities', pick 'Terminal', click Open.",
            "  2. Type your Mac password if it asks.",
            "",
            "If a box pops up saying Terminal must 'Quit & Reopen', click it,",
            "then run XTernal again.",
        ])
        if not _wait_for(ACCESS_EXPR, 300, "Accessibility"):
            _box("Didn't see it switch on", [
                "Quit Terminal completely (Command + Q), then run XTernal again.",
            ])
            log.warning("preflight: accessibility not granted within the wait")
            return 3

    # -- 3. Input Monitoring (hotkeys) -- optional ------------------------------
    listen = native.listen_event_access()
    log.info("preflight: input monitoring = %s", listen)
    if listen is False:
        native.request_permissions()
        _open(PANE_INPUT_MONITORING)
        _box("ONE-TIME STEP: let the F1 key work", [
            "A Settings window just opened on 'Input Monitoring'.",
            "",
            "  1. Find 'Terminal' and switch it ON (same as before).",
            "  2. If it says Terminal must 'Quit & Reopen', click it,",
            "     then run XTernal again.",
            "",
            "(Skips itself after 2 minutes -- the macro still works, you'd just",
            " have to use the Start button instead of F1.)",
        ])
        if not _wait_for(LISTEN_EXPR, 120, "Input Monitoring"):
            print(f"  {YELLOW}Skipping -- use the Start button instead of F1.{OFF}")
            log.warning("preflight: input monitoring not granted; continuing")

    print(f"{GREEN}Setup checks passed.{OFF}")
    return 0
