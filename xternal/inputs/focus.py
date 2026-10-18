"""
XTernal for Mac -- "is Roblox the frontmost app?"

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

CGEventPost delivers to whatever app is in front. Start the macro from its own
window and the cast's held left button lands on the macro's buttons; Cmd-Tab
away mid-session and the shake's Enter spam goes into a chat box. The guard
polls the window server's front-to-back window list (no permission needed for
owner pids) and reports whether the frontmost normal window is Roblox's.
"""

from __future__ import annotations

import threading

from ..log import get_logger
from ..macos import native

log = get_logger()

POLL_S = 0.3


def frontmost_window_owner() -> tuple[int, str]:
    """(pid, app name) of the frontmost normal (layer 0) window."""
    for w in native.list_windows(on_screen_only=True):
        if w["layer"] == 0 and w["w"] > 50 and w["h"] > 50:
            return int(w["pid"]), w["owner"]
    return 0, ""


class FocusGuard:
    """Tracks whether the game is frontmost. Thread-safe to read."""

    def __init__(self, pid_getter, on_change=None, enabled: bool = True):
        self._pid_getter = pid_getter
        self.supported = enabled and native.available()
        self.active_app = ""
        self._focused = True
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def game_focused(self) -> bool:
        return self._focused if self.supported else True

    def _query(self) -> bool:
        pid = self._pid_getter() or 0
        if not pid:
            return True  # no game to protect; never block on a guess
        front_pid, owner = frontmost_window_owner()
        self.active_app = owner
        if not front_pid:
            return True
        return front_pid == pid

    def _run(self) -> None:
        failed = False
        while not self._stop.wait(POLL_S):
            try:
                focused = self._query()
                failed = False
            except Exception:
                if not failed:
                    log.exception("focus check failed (input will not be paused)")
                failed = True
                focused = True
            if focused != self._focused:
                self._focused = focused
                if self._on_change:
                    try:
                        self._on_change(focused)
                    except Exception:
                        log.exception("focus change handler failed")

    def start(self) -> None:
        if not self.supported or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="focus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


class FocusGatedSender:
    """Wraps a sender so presses only go out while Roblox is frontmost.

    Releases always pass, so nothing is ever left held down; losing focus
    releases everything at once.
    """

    _RELEASE_WORDS = (" up}",)

    def __init__(self, inner, guard: FocusGuard):
        self.inner = inner
        self.guard = guard
        self.dropped = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _allowed(self) -> bool:
        return self.guard.game_focused

    def _drop(self, what: str) -> bool:
        self.dropped += 1
        if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
            log.debug("focus guard: dropped %s (%d dropped so far; front app %r)",
                      what, self.dropped, self.guard.active_app)
        return False

    def on_focus_lost(self) -> None:
        try:
            self.inner.release_all()
        except Exception:
            log.exception("release_all on focus loss failed")

    def send(self, text: str) -> None:
        if self._allowed() or any(w in text for w in self._RELEASE_WORDS):
            self.inner.send(text)
        else:
            self._drop(text)

    def click(self, *args, **kwargs):
        if self._allowed():
            return self.inner.click(*args, **kwargs)
        return self._drop("click")

    def click_absolute(self, *args, **kwargs):
        if self._allowed():
            return self.inner.click_absolute(*args, **kwargs)
        return self._drop("click_absolute")

    def close(self) -> None:
        self.inner.close()
