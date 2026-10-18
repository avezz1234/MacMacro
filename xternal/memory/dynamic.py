"""
XTernal for Linux -- dynamic (in-motion) offset calibration.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Some fields cannot be pinned from a single snapshot. ScreenGui.Enabled and
GuiObject.Visible are single bytes holding 0 or 1, and a static heap has dozens
of unrelated bytes that also hold 0 or 1 -- nothing in one frozen image tells
them apart.

What does tell them apart is behaviour: the reel GUI's Enabled byte is 0 while
you are not reeling and 1 while you are. So this module samples the reel GUI
over a real fishing cycle and keeps only the offsets whose value tracks that
transition.

The tricky part is knowing when reeling is actually happening, without the flag
being solved for. That ground truth comes from geometry, which is already
calibrated: while a reel minigame is running, the player bar slides around every
frame. A player bar whose position keeps changing means "reeling now", and a
static one means "not reeling" -- an independent signal that never relies on the
byte being searched for.
"""

from __future__ import annotations

import time

from .offsets import Offsets
from .proc import ProcessMemory
from .rbx import RobloxReader

#: How much of each object to snapshot. Comfortably past every GuiObject field.
SNAPSHOT_BYTES = 0x1400

#: Absolute-pixel movement of the player bar between samples that counts as
#: "the minigame is live". Chosen well above float jitter and well below the
#: travel of an actual reel.
MOVEMENT_EPSILON = 0.75


class DynamicCalibrator:
    def __init__(self, mem: ProcessMemory, offsets: Offsets, progress=None):
        self.mem = mem
        self.offsets = offsets
        self.reader = RobloxReader(mem, offsets)
        self._progress = progress
        # offset -> observed values, split by whether reeling was in progress
        self.active: dict[int, set] = {}
        self.inactive: dict[int, set] = {}
        self.samples_active = 0
        self.samples_inactive = 0

    def _say(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def _reel_guis(self) -> list[int]:
        pg = self.reader.find_player_gui()
        if not pg:
            return []
        return [
            child
            for child in self.reader.read_children(pg)
            if self.reader.read_instance_name(child) == "reel"
            and self.reader.read_class_name(child) == "ScreenGui"
        ]

    def _playerbar_x(self, reel_gui: int):
        bar = self.reader.find_child_by_name(reel_gui, "bar")
        if not bar:
            return None
        playerbar = self.reader.find_child_by_name(bar, "playerbar")
        if not playerbar:
            return None
        x, _ = self.reader.read_absolute_position(playerbar)
        return x

    def run(self, data_model: int, seconds: int = 90) -> dict:
        """Sample until enough of both states have been seen, or time runs out."""
        self.reader.set_data_model(data_model)

        self._say(
            "Cast and reel normally. Watching the reel GUI for up to "
            f"{seconds}s..."
        )

        deadline = time.time() + seconds
        last_x: dict[int, float] = {}
        moved_at: dict[int, float] = {}

        while time.time() < deadline:
            for reel_gui in self._reel_guis():
                x = self._playerbar_x(reel_gui)
                snapshot = self.mem.read(reel_gui, SNAPSHOT_BYTES)
                if snapshot is None:
                    continue

                reeling = False
                if x is not None:
                    previous = last_x.get(reel_gui)
                    if previous is not None and abs(x - previous) >= MOVEMENT_EPSILON:
                        moved_at[reel_gui] = time.time()
                    last_x[reel_gui] = x
                    # Treat the two seconds after any movement as "reeling", so a
                    # bar that momentarily sits still mid-fight is not mislabelled.
                    reeling = (time.time() - moved_at.get(reel_gui, 0)) < 2.0

                bucket = self.active if reeling else self.inactive
                for offset in range(SNAPSHOT_BYTES):
                    bucket.setdefault(offset, set()).add(snapshot[offset])
                if reeling:
                    self.samples_active += 1
                else:
                    self.samples_inactive += 1

            if self.samples_active and self.samples_inactive:
                if self.samples_active > 40 and self.samples_inactive > 40:
                    break
            time.sleep(0.05)

        self._say(
            f"Collected {self.samples_active} reeling / "
            f"{self.samples_inactive} idle samples."
        )
        return self.solve()

    def solve(self) -> dict:
        """Offsets that were exactly 1 while reeling and exactly 0 while not."""
        found: dict[str, int] = {}
        if not (self.samples_active and self.samples_inactive):
            return found

        candidates = []
        for offset, active_values in self.active.items():
            idle_values = self.inactive.get(offset)
            if not idle_values:
                continue
            if active_values == {1} and idle_values == {0}:
                candidates.append(offset)

        if candidates:
            # The lowest such offset is the ScreenGui's own Enabled flag; any
            # higher ones belong to nested state that happens to correlate.
            found["ScreenGuiEnabled"] = candidates[0]
            found["_candidates"] = candidates
        return found


def calibrate_dynamic(
    mem: ProcessMemory, offsets: Offsets, data_model: int, seconds: int = 90, progress=None
) -> dict:
    return DynamicCalibrator(mem, offsets, progress=progress).run(data_model, seconds)
