"""
XTernal for Mac -- global hotkeys.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of the HotkeyManager class in shared/Hotkeys.ahk.

AutoHotkey installs a keyboard hook; the Linux port reads evdev. On macOS the
least fragile option without pyobjc is to poll the hardware key state
(CGEventSourceKeyState on the HID state) every 25 ms and fire on the
up->down edge. Polling the *hardware* state means the macro's own synthetic
keystrokes can never trigger a hotkey, and there is no event tap that macOS
can silently disable when it is slow.

Needs Input Monitoring for Terminal on recent macOS. Like AHK hotkeys under a
game, the key is not swallowed -- the game sees it too.

On laptops, F1-F5 are brightness/etc. by default: press fn+F1, or turn on
"Use F1, F2, etc. keys as standard function keys" in Keyboard settings.
"""

from __future__ import annotations

import threading

from ..log import get_logger
from ..macos import native
from .keycodes import CODE_TO_NAME, key_code

log = get_logger()

POLL_S = 0.025


class HotkeyListener:
    def __init__(self):
        self._bindings: dict[int, callable] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str = ""

    # -- registration -----------------------------------------------------

    def register(self, key_name: str, callback) -> bool:
        code = key_code(key_name)
        if code is None:
            log.warning("hotkey %r is not a known key name; ignored", key_name)
            return False
        with self._lock:
            self._bindings[code] = callback
        return True

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def register_all(self, hotkeys: dict, handlers: dict) -> None:
        self.clear()
        for action, callback in handlers.items():
            name = (hotkeys.get(action) or "").strip()
            if name and self.register(name, callback):
                log.info("hotkey %s -> %s", name, action)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        if self._thread is not None:
            return True
        if not native.available():
            self.error = f"hotkeys unavailable: {native.load_error()}"
            return False
        if native.listen_event_access() is False:
            # Still start: on some versions polling works without it.
            self.error = (
                "Input Monitoring is not allowed for Terminal, so F1-F5 may not "
                "work. Use the buttons, or allow it in System Settings > Privacy "
                "& Security > Input Monitoring."
            )
            log.warning(self.error)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _run(self) -> None:
        down: dict[int, bool] = {}
        while not self._stop.wait(POLL_S):
            with self._lock:
                bindings = dict(self._bindings)
            for code, callback in bindings.items():
                try:
                    pressed = native.key_is_down(code)
                except Exception:
                    log.exception("hotkey poll failed; hotkeys stopped")
                    return
                if pressed and not down.get(code):
                    log.debug("hotkey pressed: %s", CODE_TO_NAME.get(code, code))
                    try:
                        callback(CODE_TO_NAME.get(code, str(code)))
                    except Exception:
                        log.exception("hotkey handler failed")
                down[code] = pressed


def available() -> bool:
    return native.available()
