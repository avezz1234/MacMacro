"""
XTernal for Mac -- Stellarwave Melody sign clicker.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Linux-port addition (kept on Mac); the Windows build has no equivalent.

During a Stellarwave reel the bar grows a `signContainer` holding twelve
TextButtons, sign1..sign12, each parked at a fixed spot along the bar. A round
pops a run of them in one at a time (size 0 -> ~102 px over ~0.4 s, a new one
every ~0.2 s, random order), and they have to be clicked in the order they
appeared before the round times out and every sign shrinks away together.

So the whole job is: watch each sign's AbsoluteSize, append it to the round's
order the moment it goes from zero to non-zero, and click each one exactly once
in that order. A pressed sign does not change size, and a stray click (twice on
one sign, or out of order) is penalised, so nothing is ever re-clicked.
"""

from __future__ import annotations

from ..inputs import window as wincursor
from ..log import get_logger
from .game import now_ms

log = get_logger()

SIGN_COUNT = 12

#: A sign this wide (px) is considered "up". Below it the sign is still
#: popping in, and a click on a 10 px dot is a click on the bar instead.
MIN_CLICK_SIZE = 60.0

#: Once every visible sign has stopped existing for this long the round is over.
ROUND_IDLE_RESET_MS = 400

#: Defaults for main.stellarwave_start_delay_ms / stellarwave_click_gap_ms. The
#: game drops clicks that arrive faster than it can process them, so the first
#: click waits this long after the round's first sign appears, and each later
#: click waits this long after the previous one.
START_DELAY_MS = 0
CLICK_GAP_MS = 0


class StellarwaveSigns:
    def __init__(self, engine):
        self.engine = engine
        #: Whether any sign round showed up during the current catch. Survives
        #: reset() (which runs per round); the engine clears it per cast.
        self.seen_this_catch = False
        self.reset()

    def reset(self) -> None:
        self.sizes: dict[str, float] = {}
        self.order: list[str] = []
        self.attempted: set[str] = set()
        self.last_seen_at = 0
        self.round_started_at = 0
        self.last_click_at = 0

    @property
    def game(self):
        return self.engine.game

    def _container(self, ctx) -> int:
        bar = ctx.get("bar") if ctx else 0
        return self.game.r.find_child_by_name(bar, "signContainer") if bar else 0

    def _read_signs(self, container: int) -> dict[str, tuple]:
        """{name: (x, y, w, h)} for every sign1..sign12 under the container."""
        out = {}
        for child, name in self.game.r.named_children(container):
            if name.startswith("sign") and name[4:].isdigit():
                out[name] = self.game.r.read_absolute_rect(child)
        return out

    def update(self, ctx) -> bool:
        """Advance the round by one step. Returns True if it owns the mouse now.

        The caller skips its normal reeling for this tick when this returns
        True, since a sign click and a held reel button share the left button.
        """
        container = self._container(ctx)
        if not container:
            self._end_round("container gone")
            return False
        signs = self._read_signs(container)
        now = now_ms()

        visible = False
        for name, (_x, _y, w, _h) in signs.items():
            prev = self.sizes.get(name, 0.0)
            if w > 0.5:
                visible = True
                if prev <= 0.5 and name not in self.order:
                    if not self.order:
                        self.round_started_at = now
                        self.seen_this_catch = True
                    self.order.append(name)
            self.sizes[name] = w

        if visible:
            self.last_seen_at = now
        elif self.order and now - self.last_seen_at >= ROUND_IDLE_RESET_MS:
            self._end_round("round over")
            return False

        # Exactly one click per sign, in order. A pressed sign does not collapse,
        # so there is no confirmation to wait on yet, and a second click on a
        # taken sign (or one out of order) costs the round -- so never re-click.
        target = next((n for n in self.order if n not in self.attempted), None)
        if target is None or target not in signs:
            return False  # everything shown so far is clicked; keep reeling
        x, y, w, h = signs[target]
        if w < MIN_CLICK_SIZE:
            return True  # next in order is still popping in; hold off reeling
        if now - self.round_started_at < self._setting("stellarwave_start_delay_ms", START_DELAY_MS):
            return True
        if self.last_click_at and now - self.last_click_at < self._setting(
            "stellarwave_click_gap_ms", CLICK_GAP_MS
        ):
            return True

        self.engine.release_mouse(force=True)
        cx, cy = self.engine.stellarwave_screen_point(x + w / 2.0, y + h / 2.0)
        if not wincursor.click_gui_point(self.engine, cx, cy):
            log.warning("stellarwave: Roblox window not found -- cannot click signs")
            self.attempted.update(self.order)
            return False
        self.attempted.add(target)
        log.info("stellarwave: click %s", target)
        # The gap counts from when the click finished.
        self.last_click_at = now_ms()
        return True

    def _setting(self, key: str, default: int) -> int:
        try:
            return max(0, int(float(self.engine.main.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _end_round(self, why: str) -> None:
        if self.order:
            log.info(
                "stellarwave: %s -- order %s, clicked %d/%d",
                why,
                ",".join(n[4:] for n in self.order),
                len(self.attempted),
                len(self.order),
            )
        self.reset()
