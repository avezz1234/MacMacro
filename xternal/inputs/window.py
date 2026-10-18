"""
XTernal for Mac -- Roblox window geometry and GUI-to-screen clicks.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Stands in for the Linux port's hyprcursor.py. A few features (the Stellarwave
sign clicker, re-centring the cursor) click a GUI object whose position comes
from memory as a Roblox AbsolutePosition. To click it, that has to become a
global macOS screen point:

    screen_x = window_x + gui_x * scale
    screen_y = window_y + titlebar + (gui_y + inset) * scale

  window_x/y, w, h  the Roblox window's frame from the window server (points)
  scale             window width / Roblox viewport width -- 1.0 normally, other
                    values on displays where Roblox renders in pixels
  titlebar          window height minus the scaled viewport height (0 fullscreen)
  inset             Roblox's top-bar inset: AbsolutePosition y=0 is *below* it;
                    measured as full viewport height minus the shortest ScreenGui

Every input to that formula is logged when it changes, so if clicks land in
the wrong place the log says exactly why. main/user stellarwave_offset_x/y are
still added on top for fine adjustment.
"""

from __future__ import annotations

import time

from ..log import get_logger
from ..macos import native

log = get_logger()

WINDOW_TTL_S = 2.0
METRICS_TTL_S = 5.0
SETTLE_S = 0.05
HOLD_MS = 50

_win_cache: tuple[float, int, dict | None] = (0.0, 0, None)
_metrics_cache: tuple[float, tuple | None] = (0.0, None)
_last_logged = None


def roblox_window(pid: int) -> dict | None:
    """The largest normal on-screen window owned by `pid` (cached briefly)."""
    global _win_cache
    at, cached_pid, win = _win_cache
    if pid == cached_pid and time.monotonic() - at < WINDOW_TTL_S:
        return win
    best = None
    try:
        for w in native.list_windows(on_screen_only=True):
            if w["pid"] != pid or w["layer"] != 0:
                continue
            if best is None or w["w"] * w["h"] > best["w"] * best["h"]:
                best = w
    except Exception:
        log.exception("window list failed")
    _win_cache = (time.monotonic(), pid, best)
    return best


def gui_metrics(reader):
    """(viewport_w, full_h, shortest_h) in Roblox GUI units, from the ScreenGuis."""
    global _metrics_cache
    at, cached = _metrics_cache
    if cached is not None and time.monotonic() - at < METRICS_TTL_S:
        return cached
    result = None
    try:
        pg = reader.find_player_gui()
        widths, heights = [], []
        for sg in reader.read_children(pg) if pg else []:
            if reader.read_class_name(sg) != "ScreenGui":
                continue
            w, h = reader.read_absolute_size(sg)
            if 100 <= w <= 20000 and 100 <= h <= 20000:
                widths.append(round(w))
                heights.append(h)
        if widths:
            width = max(set(widths), key=widths.count)  # the common viewport width
            result = (float(width), max(heights), min(heights))
    except Exception:
        log.exception("reading GUI viewport size failed")
    _metrics_cache = (time.monotonic(), result)
    return result


def _pid(engine) -> int:
    client = getattr(engine.link, "client", None)
    return client.pid if client else 0


def gui_to_screen(engine, gx: float, gy: float):
    """A Roblox AbsolutePosition point -> global screen point, or None."""
    global _last_logged
    win = roblox_window(_pid(engine))
    if win is None:
        log.warning("gui_to_screen: Roblox window not found on screen")
        return None
    metrics = gui_metrics(engine.game.r)
    if metrics is None:
        scale, title, inset = 1.0, 0.0, 0.0
    else:
        gui_w, full_h, short_h = metrics
        scale = win["w"] / gui_w if gui_w else 1.0
        title = max(0.0, win["h"] - full_h * scale)
        inset = max(0.0, full_h - short_h)
    sx = win["x"] + gx * scale
    sy = win["y"] + title + (gy + inset) * scale
    state = (round(win["x"]), round(win["y"]), round(win["w"]), round(win["h"]),
             metrics and tuple(round(m) for m in metrics))
    if state != _last_logged:
        _last_logged = state
        log.info(
            "click mapping: window %.0fx%.0f at (%.0f,%.0f), gui viewport %s, "
            "scale %.3f, titlebar %.0f, inset %.0f",
            win["w"], win["h"], win["x"], win["y"],
            "x".join(f"{m:.0f}" for m in metrics) if metrics else "unknown",
            scale, title, inset,
        )
    return sx, sy


def click_gui_point(engine, gx: float, gy: float) -> bool:
    """Click a Roblox GUI point (AbsolutePosition space)."""
    point = gui_to_screen(engine, gx, gy)
    if point is None:
        return False
    log.debug("click gui (%.0f,%.0f) -> screen (%.0f,%.0f)", gx, gy, *point)
    return bool(engine.sender.click_absolute(round(point[0]), round(point[1]), hold_ms=HOLD_MS))


def center_cursor(engine) -> bool:
    """Put the cursor in the middle of the Roblox window."""
    win = roblox_window(_pid(engine))
    if win is None:
        return False
    return bool(engine.sender.move_absolute(round(win["x"] + win["w"] / 2),
                                            round(win["y"] + win["h"] / 2)))
