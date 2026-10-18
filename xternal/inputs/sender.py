"""
XTernal for Mac -- synthetic input via CoreGraphics events.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Replaces AutoHotkey's Send / Click / MouseMove (and the Linux port's uinput
devices) with CGEventPost at the HID tap: events enter the system input stream
like real hardware and go to the frontmost app.

Needs the Accessibility permission for the app that launched the macro
(Terminal). Without it macOS silently drops every event, so the controller
checks the permission up front and says so instead.

The interface is the Linux InputSender's, method for method, so the engine
and the rod controllers are unchanged.
"""

from __future__ import annotations

import time

from ..log import get_logger
from ..macos import native
from .keycodes import key_code, mouse_code, parse_send_string

log = get_logger()

_DOWN = {0: native.kCGEventLeftMouseDown, 1: native.kCGEventRightMouseDown,
         2: native.kCGEventOtherMouseDown}
_UP = {0: native.kCGEventLeftMouseUp, 1: native.kCGEventRightMouseUp,
       2: native.kCGEventOtherMouseUp}
_DRAG = {0: native.kCGEventLeftMouseDragged, 1: native.kCGEventRightMouseDragged,
         2: native.kCGEventOtherMouseDragged}


class InputError(RuntimeError):
    pass


class InputSender:
    """The AHK input verbs the ported macro calls, backed by CGEvent."""

    def __init__(self):
        if not native.available():
            raise InputError(f"macOS input unavailable: {native.load_error()}")
        self.source = native.event_source()
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()
        self.sent = 0  # for the log: how many events went out this session

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Release anything still held -- a button left down survives the app."""
        self.release_all()

    def release_all(self) -> None:
        for code in list(self._held_buttons):
            self.mouse_up_code(code)
        for code in list(self._held_keys):
            self.key_up_code(code)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- keyboard ---------------------------------------------------------

    def key_down_code(self, code: int) -> None:
        native.post_key(self.source, code, True)
        self._held_keys.add(code)
        self.sent += 1

    def key_up_code(self, code: int) -> None:
        native.post_key(self.source, code, False)
        self._held_keys.discard(code)
        self.sent += 1

    def tap_key(self, name: str, hold_ms: int = 25) -> bool:
        code = key_code(name)
        if code is None:
            log.warning("tap_key: unknown key name %r", name)
            return False
        self.key_down_code(code)
        time.sleep(hold_ms / 1000.0)
        self.key_up_code(code)
        return True

    # -- mouse ------------------------------------------------------------

    def _where(self) -> tuple[float, float]:
        return native.cursor_position()

    def mouse_down_code(self, code: int) -> None:
        x, y = self._where()
        native.post_mouse(self.source, _DOWN[code], x, y, code, click_state=1)
        self._held_buttons.add(code)
        self.sent += 1

    def mouse_up_code(self, code: int) -> None:
        x, y = self._where()
        native.post_mouse(self.source, _UP[code], x, y, code, click_state=1)
        self._held_buttons.discard(code)
        self.sent += 1

    def mouse_down(self, button: str = "left") -> None:
        code = mouse_code(button)
        if code is not None:
            self.mouse_down_code(code)

    def mouse_up(self, button: str = "left") -> None:
        code = mouse_code(button)
        if code is not None:
            self.mouse_up_code(code)

    def click(self, button: str = "left", hold_ms: int = 30) -> None:
        self.mouse_down(button)
        time.sleep(hold_ms / 1000.0)
        self.mouse_up(button)

    def _move_to(self, x: float, y: float, dx: int = 0, dy: int = 0) -> None:
        held = next(iter(self._held_buttons), None)
        if held is None:
            native.post_mouse(self.source, native.kCGEventMouseMoved, x, y, 0, dx=dx, dy=dy)
        else:
            native.post_mouse(self.source, _DRAG[held], x, y, held, dx=dx, dy=dy)
        self.sent += 1

    def move_relative(self, dx: int, dy: int) -> None:
        x, y = self._where()
        self._move_to(x + dx, y + dy, int(dx), int(dy))

    def move_absolute(self, x: int, y: int, screen_w: int = 0, screen_h: int = 0) -> bool:
        """Put the cursor at global screen point (x, y) -- macOS points."""
        cx, cy = self._where()
        self._move_to(float(x), float(y), int(x - cx), int(y - cy))
        return True

    def click_absolute(self, x: int, y: int, screen_w: int = 0, screen_h: int = 0,
                       hold_ms: int = 50) -> bool:
        """ReliableScreenClick(): arrive, wiggle so Roblox sees motion, then click.

        Roblox ignores a click that lands in the same frame the cursor
        teleports, so the cursor is nudged with real motion events and given a
        few frames to settle, and the press is held across frames.
        """
        self.move_absolute(x, y)
        time.sleep(0.016)
        for ddx in (2, -2):
            self.move_relative(ddx, 0)
            time.sleep(0.016)
        time.sleep(0.05)
        native.post_mouse(self.source, _DOWN[0], float(x), float(y), 0, click_state=1)
        self._held_buttons.add(0)
        time.sleep(hold_ms / 1000.0)
        native.post_mouse(self.source, _UP[0], float(x), float(y), 0, click_state=1)
        self._held_buttons.discard(0)
        self.sent += 2
        return True

    # -- AHK-compatible entry point ---------------------------------------

    def send(self, text: str) -> None:
        """Execute an AHK Send() string ("{Enter}", "{LButton down}", "t", ...)."""
        for verb, name in parse_send_string(text):
            code = mouse_code(name)
            if code is not None:
                if verb == "down":
                    self.mouse_down_code(code)
                elif verb == "up":
                    self.mouse_up_code(code)
                else:
                    self.click(name)
                continue
            kcode = key_code(name)
            if kcode is None:
                log.warning("send: unknown key name %r in %r", name, text)
                continue
            if verb == "down":
                self.key_down_code(kcode)
            elif verb == "up":
                self.key_up_code(kcode)
            else:
                self.tap_key(name)


class NullSender:
    """No-op stand-in so the UI and logic run without input permissions."""

    sent = 0

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return False

        return _noop

    def close(self) -> None:
        pass
