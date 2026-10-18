"""
XTernal for Mac -- AHK key names to macOS virtual keycodes.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

The fishing logic and saved settings still speak AutoHotkey key names
("{Enter}", "\\", "LButton", "F1"), so a settings.json from Windows or Linux
loads unchanged. This is the one place they become macOS kVK_* codes
(HIToolbox/Events.h). The codes are physical key positions on an ANSI layout.
"""

from __future__ import annotations

import re

#: Mouse buttons, by the AHK names the macro logic uses -> CGMouseButton.
MOUSE_BUTTONS = {
    "lbutton": 0, "left": 0,
    "rbutton": 1, "right": 1,
    "mbutton": 2, "middle": 2,
}

KEYS: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
    "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D,
    "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
    "enter": 0x24, "return": 0x24,
    "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29,
    "\\": 0x2A, "backslash": 0x2A,
    ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F,
    "tab": 0x30, "space": 0x31, "`": 0x32,
    "backspace": 0x33, "bs": 0x33,
    "esc": 0x35, "escape": 0x35,
    "lwin": 0x37, "rwin": 0x36, "command": 0x37, "cmd": 0x37,
    "shift": 0x38, "lshift": 0x38, "rshift": 0x3C,
    "capslock": 0x39,
    "alt": 0x3A, "lalt": 0x3A, "option": 0x3A, "ralt": 0x3D,
    "ctrl": 0x3B, "lctrl": 0x3B, "control": 0x3B, "rctrl": 0x3E,
    "numpaddot": 0x41, "numpadmult": 0x43, "numpadadd": 0x45,
    "numpaddiv": 0x4B, "numpadenter": 0x4C, "numpadsub": 0x4E,
    "numpad0": 0x52, "numpad1": 0x53, "numpad2": 0x54, "numpad3": 0x55,
    "numpad4": 0x56, "numpad5": 0x57, "numpad6": 0x58, "numpad7": 0x59,
    "numpad8": 0x5B, "numpad9": 0x5C,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60,
    "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D,
    "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71,
    "home": 0x73, "pgup": 0x74, "delete": 0x75, "del": 0x75,
    "end": 0x77, "pgdn": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}

CODE_TO_NAME: dict[int, str] = {}
for _name, _code in KEYS.items():
    CODE_TO_NAME.setdefault(_code, _name)


def key_code(name: str) -> int | None:
    """Resolve a single AHK-style key name to a macOS virtual keycode."""
    if not name:
        return None
    return KEYS.get(name.strip().lower())


def is_mouse_button(name: str) -> bool:
    return bool(name) and name.strip().lower() in MOUSE_BUTTONS


def mouse_code(name: str) -> int | None:
    return MOUSE_BUTTONS.get(name.strip().lower()) if name else None


_TOKEN = re.compile(r"\{([^}]+)\}|(.)", re.DOTALL)


def parse_send_string(text: str):
    """Split an AHK Send() string into ('key'|'down'|'up', name) actions.

    Handles the forms the macro emits: "{Enter}", "\\", "t",
    "{LButton down}", "{LButton up}", "{A}".
    """
    actions = []
    for match in _TOKEN.finditer(text):
        braced, literal = match.group(1), match.group(2)
        if braced is not None:
            parts = braced.strip().split()
            name = parts[0]
            verb = parts[1].lower() if len(parts) > 1 else ""
            if verb == "down":
                actions.append(("down", name))
            elif verb == "up":
                actions.append(("up", name))
            else:
                actions.append(("key", name))
        elif literal is not None and literal.strip():
            actions.append(("key", literal))
    return actions
