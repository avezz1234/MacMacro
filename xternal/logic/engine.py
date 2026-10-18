"""
XTernal for Mac -- the macro engine (phase machine, totem, appraisal).

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of MacroLoop and its phase handlers in shared/Fish.ahk, the auto-totem
workflow, and shared/Appraise.ahk. AutoHotkey drove these off SetTimer against
a single-threaded message loop; here the same cadence comes from tick() being
called by the UI's own loop, which keeps the whole engine single-threaded and
free of locking exactly as the original was.
"""

from __future__ import annotations

import time

from ..settings import MIN_CAST_TIMEOUT_MS
from .fish import (
    APPRAISE_FIXED_RETRY_MS,
    APPRAISE_RESOLVE_TIMEOUT_MS,
    APPRAISE_SUBVALUES_MAX_RETRIES,
    AUTO_TOTEM_WAIT_MS,
    BellonaController,
    FishingController,
    LullabyController,
    MacroState,
    PinionController,
    REQUIEM_ACTION_DELAY_MS,
    RequiemController,
    TranquilityController,
)
from .catchlog import CatchLog
from .catchsink import CatchSink
from ..inputs import window as wincursor
from ..log import get_logger
from .stellarwave import StellarwaveSigns
from .game import (
    FischGame,
    GameLink,
    is_bellona_rod,
    is_dreambreaker_rod,
    is_lullaby_rod,
    is_pinion_rod,
    is_requiem_rod,
    is_tranquility_rod,
    now_ms,
)

log = get_logger()

#: How often an already-known rod is re-read from the hotbar.
ROD_RECHECK_MS = 1000


#: With main.early_cast, how often to re-press while the game is still
#: finishing the last catch and has not shown the cast power bar yet.
EARLY_CAST_REPRESS_MS = 250

#: Any rod: a cast held this long without the power bar appearing was
#: swallowed by the game, so release and press again.
CAST_REPRESS_MS = 1000

#: Rod values/state while a catch is still ending (reeling, fight won, wrap-up).
ROD_STATES_FINISHING = (7, 8, 9)

#: Shake-phase stall guard. A healthy wait keeps moving: the lure lands, the
#: shake button keeps respawning under the Enter spam, then the reel opens.
#: These spot a wait that has quietly stopped moving and fix it on the spot,
#: long before cast_timeout_ms gives up on the cast.
STALL_CHECK_MS = 250
#: The throw should be out by now; a rod still reading ready (2) or charging
#: (3) means it never left, so recast instead of shaking at nothing.
STALL_THROW_MS = 1500
ROD_STATES_NOT_THROWN = (2, 3)
#: Same shake button sat under the Enter spam this long: Enter is not reaching
#: it (UI navigation lost its selection, e.g. after a mouse click). Toggle UI
#: navigation; repeated nudges alternate, so one of them always lands on "on".
STALL_SHAKE_STUCK_MS = 1500
#: No shake button at all this long after the throw: the lure is dead in the
#: water. Only trusted once a shake button has been read this session, so a
#: changed GUI path can never turn this into a recast loop.
STALL_NO_SHAKE_MS = 6000

#: Rod values/state once the line is out (thrown, lure waiting).
ROD_STATES_THROWN = (4, 5)

#: Self-recovery. Tuned off ~14.8k logged catches: median cast->catch is 10 s,
#: p99 is 50 s, and nearly everything past ~90 s was a session that had quietly
#: died. A cycle that runs this long without ending is treated as stuck.
RECOVER_CYCLE_MS = 90_000
#: ...unless it is mid-reel and the progress bar is still moving; a long fight
#: is not a stall. This is the hard cap even then.
RECOVER_CYCLE_HARD_MS = 180_000
#: A reel whose progress has not moved for this long is stuck.
RECOVER_REEL_FROZEN_MS = 25_000
#: Reeling phases, whose progress bar is watched for movement.
REEL_PHASES = ("FISHING", "BELLONA", "TRANQUILITY", "LULLABY")
#: How long to wait for an equip/unequip to show up in memory.
EQUIP_CONFIRM_MS = 1200

#: Assisted catch: which settings switch covers each phase.
ASSIST_PART_FOR_PHASE = {
    "CASTING": "cast",
    "CASTED": "cast",
    "SHAKE": "shake",
    "TRANQUILITY": "minigames",
    "LULLABY": "minigames",
    "BELLONA": "minigames",
}


class MacroEngine:
    """Owns macro state and drives one tick of the phase machine at a time."""

    def __init__(self, link: GameLink, sender, settings: dict, webhook=None):
        self.link = link
        self.game = FischGame(link)
        self.sender = sender
        self.settings = settings
        self.webhook = webhook
        self.state = MacroState()
        self.catches = CatchLog()
        self.catch_sink = CatchSink()
        self.rod = ""
        self.dreambreaker = False
        self.controller = FishingController(self)
        self.stellarwave = StellarwaveSigns(self)
        self.status_line = "OFF"
        self._hotbar_init_at = 0
        self._rod_checked_at = 0
        # Shake-phase stall guard bookkeeping (see _shake_stall_guard).
        self._stall_cast = 0
        self._stall_checked_at = 0
        self._stall_sig = None
        self._stall_sig_since = 0
        self._stall_shake_seen = False
        self._shake_ui_known = False
        # Self-recovery bookkeeping (see recover / _recovery_watch).
        self.recover_level = 0
        self.recover_count = 0
        self._reel_progress_last = None
        self._reel_progress_moved_at = 0
        # Screen size for the absolute appraise click; filled in by the UI.
        self.screen_size = (0, 0)

    # -- settings shortcuts ----------------------------------------------

    @property
    def main(self) -> dict:
        return self.settings["main"]

    @property
    def userprefs(self) -> dict:
        return self.settings["user"]

    @property
    def webhook_cfg(self) -> dict:
        return self.settings["webhook"]

    # -- assisted catch ---------------------------------------------------

    def assists(self, part: str) -> bool:
        """Does the macro play this part? Always, unless assisted catch is on."""
        if not self.main.get("assist_enabled"):
            return True
        return bool(self.main.get(f"assist_{part}"))

    def reel_part(self) -> str:
        """The plain reel bar, or a rod whose reel plays differently."""
        return "reel" if type(self.controller) is FishingController else "minigames"

    def phase_part(self, phase: str) -> str:
        if phase == "FISHING":
            return self.reel_part()
        return ASSIST_PART_FOR_PHASE.get(phase, "")

    def _bite_visible(self) -> bool:
        """Anything showing that the line is out: shake prompt or any reel."""
        if self.game.get_shake_button() or self.game.has_active_fishing_context():
            return True
        if is_tranquility_rod(self.rod) and self.game.get_tranquility_lane_container():
            return True
        return is_lullaby_rod(self.rod) and self.game.is_metronome_active()

    def watch_player_cast(self) -> None:
        """Assisted catch with the cast left to the player: wait for their throw."""
        s = self.state
        s.early_cast_pending = False
        # Check for the throw first: the power bar can linger after the cast,
        # and waiting on it to go would miss the bite entirely.
        if self.game.read_rod_state() in ROD_STATES_THROWN or self._bite_visible():
            s.power_percent = ""
            s.cast_released_at = now_ms()
            s.last_shaked_at = 0
            s.phase = "SHAKE"
            return
        bar = self.game.resolve_power_bar()
        if bar:
            s.cast_bar_seen = True
            s.power_percent = f"{self.game.read_power_bar_percent(bar):.1f}"
        else:
            s.power_percent = ""

    # -- input helpers (with the original's action-delay gating) ----------

    def effective_action_delay_ms(self) -> int:
        """The delay actually enforced this tick.

        Normally the user's setting, but Requiem is forced to 165 ms while
        equipped. Session-only on purpose: the saved value is never rewritten,
        so tracking on every other rod is untouched.
        """
        if is_requiem_rod(self.rod):
            return REQUIEM_ACTION_DELAY_MS
        return int(self.main["fishing_action_delay_ms"])

    def hold_mouse(self) -> None:
        s = self.state
        if s.is_holding:
            return
        delay = self.effective_action_delay_ms()
        if (
            s.phase == "FISHING"
            and delay > 0
            and s.last_action_at
            and (now_ms() - s.last_action_at) < delay
        ):
            return
        self.sender.send("{LButton down}")
        s.is_holding = True
        s.last_action_at = now_ms()

    def release_mouse(self, force: bool = False) -> None:
        s = self.state
        if not s.is_holding:
            return
        delay = self.effective_action_delay_ms()
        if (
            not force
            and s.phase == "FISHING"
            and delay > 0
            and s.last_action_at
            and (now_ms() - s.last_action_at) < delay
        ):
            return
        self.sender.send("{LButton up}")
        s.is_holding = False
        s.last_action_at = now_ms()

    def hold_right_mouse(self) -> None:
        s = self.state
        if s.is_holding_right:
            return
        delay = self.effective_action_delay_ms()
        if delay > 0 and s.last_right_action_at and (now_ms() - s.last_right_action_at) < delay:
            return
        self.sender.send("{RButton down}")
        s.is_holding_right = True
        s.last_right_action_at = now_ms()

    def release_right_mouse(self, force: bool = False) -> None:
        s = self.state
        if not s.is_holding_right:
            return
        delay = self.effective_action_delay_ms()
        if not force and delay > 0 and s.last_right_action_at and \
           (now_ms() - s.last_right_action_at) < delay:
            return
        self.sender.send("{RButton up}")
        s.is_holding_right = False
        s.last_right_action_at = now_ms()

    def release_all_fishing_mouse(self, force: bool = False) -> None:
        self.release_mouse(force)
        self.release_right_mouse(force)

    def select_hotbar_slot(self, slot_key: str) -> bool:
        if not slot_key:
            return False
        self.sender.send("{" + slot_key + "}")
        time.sleep(0.075)
        return True

    def ensure_rod_equipped(self) -> bool:
        if self.game.is_rod_equipped():
            return True
        return self.select_hotbar_slot("1")

    # -- self-recovery ----------------------------------------------------

    def _wait_for(self, check, timeout_ms: int = EQUIP_CONFIRM_MS) -> bool:
        """Poll a memory check until it holds or the timeout runs out."""
        deadline = now_ms() + timeout_ms
        while True:
            try:
                if check():
                    return True
            except Exception:
                pass
            if now_ms() >= deadline:
                return False
            time.sleep(0.05)

    def reequip_rod(self) -> bool:
        """Put the rod away and take it out again, confirming each step in memory.

        Unequipping cancels whatever the rod was stuck in (a dead lure, a reel
        that never closed, a cast the server forgot), so the next cast starts
        from a clean tool.
        """
        self.release_all_fishing_mouse(force=True)
        if self.game.is_rod_equipped():
            self.select_hotbar_slot("1")
            if not self._wait_for(lambda: not self.game.is_anything_equipped()):
                log.info("recover: rod did not unequip, pressing again")
                self.select_hotbar_slot("1")
                self._wait_for(lambda: not self.game.is_anything_equipped())
            time.sleep(0.3)
        for _ in range(3):
            if not self.game.is_rod_equipped():
                self.select_hotbar_slot("1")
            if self._wait_for(self.game.is_rod_equipped):
                time.sleep(0.25)  # let the tool's GUI finish loading in
                self.game._rod_state_addr = 0
                return True
        log.warning("recover: rod would not re-equip")
        return False

    def recover(self, reason: str) -> None:
        """Fix a stuck cycle, escalating while the fixes keep failing.

        1: recast.  2: re-equip the rod, then recast.  3+: also reset UI
        navigation (the shake prompt and hotbar keys both depend on it).
        The level resets on the next landed fish, so one bad cast never
        escalates.
        """
        s = self.state
        self.recover_level += 1
        self.recover_count += 1
        level = self.recover_level
        log.info("recover L%d (#%d): %s [phase=%s rod_state=%s]",
                 level, self.recover_count, reason, s.phase, self.game.read_rod_state())
        self.release_all_fishing_mouse(force=True)
        self.controller.reset()
        self.stellarwave.reset()
        self.game.clear_phase_cache()
        self.game.stale_reel_gui = 0
        s.early_cast_pending = False
        if level >= 3:
            # Toggle twice: lands back where it was, but re-selects the first
            # navigable element, which is what a lost selection needs.
            self.sender.send("\\")
            time.sleep(0.1)
            self.sender.send("\\")
            time.sleep(0.1)
        if level >= 2:
            self.reequip_rod()
        else:
            self.ensure_rod_equipped()
        self.start_macro_cycle()

    def _recovery_watch(self) -> None:
        """Spot a cycle that has stopped going anywhere, whatever phase it is in."""
        s = self.state
        if not s.cycle_enabled or s.phase in ("OFF", "DONE", "FAILED", "APPRAISE"):
            return
        if s.totem_state != "IDLE" or self.main.get("assist_enabled"):
            return
        now = now_ms()
        if s.phase in REEL_PHASES:
            progress = s.progress_percent
            if progress != self._reel_progress_last or not self._reel_progress_moved_at:
                self._reel_progress_last = progress
                self._reel_progress_moved_at = now
            if now - self._reel_progress_moved_at >= RECOVER_REEL_FROZEN_MS:
                self.recover(f"reel progress frozen at {progress!r} for "
                             f"{(now - self._reel_progress_moved_at) // 1000}s")
                return
        else:
            self._reel_progress_last = None
            self._reel_progress_moved_at = 0
        if not s.cycle_started_at:
            return
        age = now - s.cycle_started_at
        reeling = s.phase in REEL_PHASES
        if (age >= RECOVER_CYCLE_MS and not reeling) or age >= RECOVER_CYCLE_HARD_MS:
            self.recover(f"cycle stuck in {s.phase} for {age // 1000}s")

    # -- cycle control ----------------------------------------------------

    def resolve_cast_threshold(self) -> float:
        mode = self.main["cast_mode"]
        if mode == "short":
            return 28.0
        if mode == "custom":
            return max(1.0, min(100.0, float(self.main["cast_power_custom"])))
        return 96.0

    def cycle_start_delay_ms(self) -> int:
        return max(0, int(self.main["pre_cast_delay_ms"]))

    def start_macro_cycle(self) -> None:
        """StartMacroCycle(): pick the controller for the rod, then cast."""
        s = self.state
        if s.phase == "OFF":
            s.totem_night_covered = False
            s.totem_pending = False
            s.totem_blocked_until_catch_end = False
            if not s.session_started_at:
                s.session_started_at = now_ms()
            if self.webhook:
                self.webhook.begin_session()
        s.cycle_started_at = now_ms()

        if is_tranquility_rod(self.rod):
            self.controller = TranquilityController(self)
        elif is_lullaby_rod(self.rod):
            self.controller = LullabyController(self)
        elif is_pinion_rod(self.rod):
            self.controller = PinionController(self)
        elif is_bellona_rod(self.rod):
            self.controller = BellonaController(self)
        elif is_requiem_rod(self.rod):
            self.controller = RequiemController(self)
        else:
            self.controller = FishingController(self)

        self.dreambreaker = is_dreambreaker_rod(self.rod)
        self.release_mouse()
        self.release_right_mouse()
        self.controller.reset()
        self.stellarwave.reset()
        self.stellarwave.seen_this_catch = False
        self.initialize_cast_cycle()

    def initialize_cast_cycle(self) -> None:
        s = self.state
        if not s.activated_ui_nav:
            # Backslash toggles Roblox's UI navigation, which the macro relies on
            # for keyboard-driven hotbar selection.
            self.sender.send("\\")
            s.activated_ui_nav = True
            time.sleep(0.05)

        s.power_percent = ""
        s.progress_percent = ""
        s.cast_started_at = now_ms()
        s.cast_released_at = 0
        s.cast_bar_seen = False
        s.fishing_lost_at = 0
        s.completion_reached = False
        s.outcome_resolved = False
        s.bellona_left_done = False
        s.bellona_right_done = False
        s.last_shaked_at = 0
        s.last_action_at = 0
        s.reel_progress_peak = None
        s.reel_progress_dropped = False
        self.game.power_bar_addr = 0
        s.cast_threshold = self.resolve_cast_threshold()
        s.cast_wait_timeout_ms = max(MIN_CAST_TIMEOUT_MS, int(self.main["cast_timeout_ms"]))
        s.fishing_end_grace_ms = 100
        s.shaking_interval_ms = int(self.main["shake_interval_ms"])
        # Snapshot the catch banner now so a landed fish is detected as a change
        # against this cycle, not against whatever was announced last.
        try:
            self.catches.arm(self.game)
        except Exception:
            pass
        s.phase = "CASTING"

    def stop_macro_cycle(self, next_phase: str = "OFF") -> None:
        s = self.state
        self.release_mouse()
        self.release_right_mouse()
        self.controller.reset()

        s.power_percent = ""
        s.cast_started_at = 0
        s.cast_released_at = 0
        s.cast_bar_seen = False
        s.progress_percent = ""
        s.fishing_lost_at = 0
        s.completion_reached = False
        s.outcome_resolved = False
        s.bellona_left_done = False
        s.bellona_right_done = False
        s.last_shaked_at = 0
        s.last_action_at = 0
        self.game.clear_phase_cache()
        s.phase = next_phase

        if next_phase == "DONE":
            s.totem_blocked_until_catch_end = False
        elif next_phase == "OFF":
            s.early_cast_pending = False
            if s.totem_state != "IDLE" and s.totem_needs_rod_reequip:
                self.select_hotbar_slot("1")
            self.reset_auto_totem_control()
            s.totem_night_covered = False
            self.clear_appraise_cache()

    def start(self) -> tuple[bool, str]:
        """StartMacro(): the hotkey/button entry point. Returns (ok, message)."""
        s = self.state
        if s.cycle_enabled:
            s.cycle_enabled = False
            if s.phase == "APPRAISE":
                self.stop_appraise_cycle("OFF", "Stopped.")
            else:
                self.stop_macro_cycle("OFF")
            return True, "Stopped."

        if not self.link.is_memory_ready():
            return False, "Roblox is not attached."
        if not self.link.is_in_fisch_game():
            return False, "You're not in Fisch yet."

        if self.is_auto_appraise_enabled():
            if s.phase in ("OFF", "DONE", "FAILED"):
                return self.start_appraise_cycle()
            return True, ""

        if not self.game.is_anything_equipped():
            self.sender.send("t")
            time.sleep(0.2)

        s.cycle_enabled = True
        if s.phase in ("OFF", "DONE", "FAILED"):
            self.start_macro_cycle()
        return True, "Started."

    # -- the tick ---------------------------------------------------------

    def tick(self) -> None:
        """MacroLoop(): one pass of the phase machine."""
        s = self.state
        if not self.link.is_memory_ready():
            return
        self.game.begin_frame()

        if s.phase != "APPRAISE" and self.update_auto_totem():
            self.status_line = self.display_status()
            return

        handlers = {
            "CASTING": self.update_casting_phase,
            "CASTED": self.update_casted_phase,
            "SHAKE": self.update_shake_phase,
            "FISHING": self.update_fishing_phase,
            "TRANQUILITY": self.update_tranquility_phase,
            "LULLABY": self.update_lullaby_phase,
            "BELLONA": self.update_bellona_phase,
            "APPRAISE": self.update_appraise_phase,
        }
        phase_before = s.phase
        if s.phase == "DONE":
            if s.cycle_enabled:
                self.start_macro_cycle()
            else:
                self.stop_macro_cycle("OFF")
        else:
            handler = handlers.get(s.phase)
            if handler:
                handler()
        self._recovery_watch()
        if s.phase != phase_before:
            if self.main.get("assist_enabled"):
                log.info(
                    "assist: %s -> %s (rod=%r controller=%s plays=%s)",
                    phase_before, s.phase, self.rod, type(self.controller).__name__,
                    self.assists(self.phase_part(s.phase)) if self.phase_part(s.phase) else "-",
                )
            else:
                log.debug("phase %s -> %s (rod=%r power=%s progress=%s)",
                          phase_before, s.phase, self.rod, s.power_percent,
                          s.progress_percent)

        if self.catches.polling:
            try:
                landed = self.catches.poll(self.game)
            except Exception:
                landed = None
            if landed is not None:
                self.on_catch_logged(landed)

        self.status_line = self.display_status()
        if s.phase != "OFF" and self.webhook:
            self.webhook.maybe_send_summary(self)

    def on_catch_logged(self, catch) -> None:
        """Override point for the UI; also fires the rare-catch webhook alert."""
        # Emit before the alert: the feed is the record external tools read, and
        # it should not depend on whether a webhook happens to be configured.
        self.catch_sink.write(catch)
        if self.webhook and self.webhook.enabled():
            threshold = 1000
            if catch.rarity_denominator >= threshold:
                self.webhook.send_alert(
                    "Rare Catch",
                    f"**{catch.display}** at **{catch.weight:g}kg** ({catch.rarity})",
                )

    def display_status(self) -> str:
        s = self.state
        if s.phase == "APPRAISE":
            return f"APPRAISE {s.appraise_state}"
        if s.totem_state != "IDLE":
            return s.totem_state
        part = self.phase_part(s.phase)
        if part and not self.assists(part):
            return f"{s.phase} · YOU"
        return s.phase

    # -- phases -----------------------------------------------------------

    def update_casting_phase(self) -> None:
        s = self.state
        s.progress_percent = ""

        if not self.assists("cast"):
            self.watch_player_cast()
            return

        delay = self.cycle_start_delay_ms()
        if delay > 0 and (now_ms() - s.cast_started_at) < delay:
            return

        rod_state = self.game.read_rod_state() if s.early_cast_pending else None
        if rod_state in ROD_STATES_FINISHING:
            # The last catch is still wrapping up. A press held from now is
            # picked up late, so stay off the button and press the moment the
            # rod reads ready (state 2).
            self.release_mouse(force=True)
        else:
            self.hold_mouse()
        if not s.cast_started_at:
            s.cast_started_at = now_ms()

        bar = self.game.resolve_power_bar()
        if not bar and s.early_cast_pending and rod_state is None:
            # Fallback when the rod state cannot be read: a held button does
            # not count as a new press, so press again until the bar shows up.
            if s.last_action_at and now_ms() - s.last_action_at >= EARLY_CAST_REPRESS_MS:
                self.release_mouse(force=True)
                self.hold_mouse()
        elif not bar and s.is_holding and s.last_action_at and (
            now_ms() - s.last_action_at >= CAST_REPRESS_MS
        ):
            # Held this long with no power bar: the game swallowed the press
            # (it came while the rod was still busy). Press it again.
            self.release_mouse(force=True)
            self.hold_mouse()
        if bar:
            s.early_cast_pending = False
        if not bar:
            s.power_percent = "---"
            if (now_ms() - s.cast_started_at) >= s.cast_wait_timeout_ms:
                s.cast_timeout_count += 1
                # Keeps the macro alive when a nuke (or anything else) eats the
                # cast bar entirely.
                if self.main["cast_on_timeout"]:
                    self.recover("cast timeout, no power bar")
                else:
                    self.stop_macro_cycle("OFF")
            return

        s.cast_bar_seen = True
        percent = self.game.read_power_bar_percent(bar)
        s.power_percent = f"{percent:.1f}"

        if percent >= s.cast_threshold:
            self.release_mouse()
            s.cast_released_at = now_ms()
            s.phase = "CASTED"
            return

        if (now_ms() - s.cast_started_at) >= s.cast_wait_timeout_ms:
            s.cast_timeout_count += 1
            if self.main["cast_on_timeout"]:
                self.recover("cast timeout, power bar never filled")
            else:
                self.stop_macro_cycle("OFF")

    def update_casted_phase(self) -> None:
        s = self.state
        s.power_percent = ""
        s.progress_percent = ""
        self.release_mouse()
        if not s.cast_released_at:
            s.cast_released_at = now_ms()
        if (now_ms() - s.cast_released_at) < int(self.main["post_cast_delay_ms"]):
            return
        s.last_shaked_at = 0
        s.phase = "SHAKE"

    def update_shake_phase(self) -> None:
        s = self.state
        s.power_percent = ""
        s.progress_percent = ""
        self.release_mouse()

        if is_tranquility_rod(self.rod) and self.game.get_tranquility_lane_container():
            s.last_shaked_at = 0
            s.fishing_lost_at = 0
            s.phase = "TRANQUILITY"
            return

        if is_lullaby_rod(self.rod) and self.game.is_metronome_active():
            s.last_shaked_at = 0
            s.fishing_lost_at = 0
            s.phase = "LULLABY"
            return

        if is_bellona_rod(self.rod):
            if any(
                c.get("fish") and c.get("playerbar")
                for c in self.game.get_reel_contexts()
            ) or self.game.has_active_fishing_context():
                s.last_shaked_at = 0
                s.fishing_lost_at = 0
                s.phase = "BELLONA"
                return
        elif self.game.has_active_fishing_context():
            s.last_shaked_at = 0
            s.fishing_lost_at = 0
            s.phase = "FISHING"
            return

        if (
            not self.assists("cast")
            and s.cast_released_at
            and now_ms() - s.cast_released_at >= STALL_THROW_MS
            and self.game.read_rod_state() in ROD_STATES_NOT_THROWN
        ):
            # The player reeled the line back in with no bite: wait for their
            # next throw.
            s.cast_started_at = now_ms()
            s.cast_released_at = 0
            s.phase = "CASTING"
            return

        if not self.assists("shake"):
            return

        if not s.last_shaked_at or (now_ms() - s.last_shaked_at) >= s.shaking_interval_ms:
            self.sender.send("{Enter}")
            s.last_shaked_at = now_ms()

        if self._shake_stall_guard():
            return

        if (
            self.assists("cast")
            and s.cast_released_at
            and (now_ms() - s.cast_released_at) >= s.cast_wait_timeout_ms
        ):
            self.recover("no bite before cast timeout")

    def _shake_stall_guard(self) -> bool:
        """Catch a shake-phase wait that has stopped moving. True if it recast."""
        s = self.state
        if not s.cast_released_at:
            return False
        now = now_ms()
        if self._stall_cast != s.cast_released_at:
            self._stall_cast = s.cast_released_at
            self._stall_checked_at = 0
            self._stall_sig = None
            self._stall_sig_since = 0
            self._stall_shake_seen = False
        if now - self._stall_checked_at < STALL_CHECK_MS:
            return False
        self._stall_checked_at = now
        waited = now - s.cast_released_at

        try:
            rod_state = self.game.read_rod_state() if waited >= STALL_THROW_MS else None
            button = self.game.get_shake_button()
            sig = self.game.shake_button_signature(button)
        except Exception:
            return False

        if waited >= STALL_THROW_MS and self.assists("cast"):
            if rod_state in ROD_STATES_NOT_THROWN:
                self.recover(f"stall guard: throw never left (rod state {rod_state})")
                return True
            if rod_state is None and not self.game.is_anything_equipped():
                self.recover("stall guard: rod unequipped while waiting")
                return True

        if sig:
            self._stall_shake_seen = self._shake_ui_known = True
            if sig != self._stall_sig:
                self._stall_sig = sig
                self._stall_sig_since = now
            elif now - self._stall_sig_since >= STALL_SHAKE_STUCK_MS:
                log.info("stall guard: shake button not responding, toggling UI nav")
                self.sender.send("\\")
                self._stall_sig_since = now
            return False

        if (
            self.assists("cast")
            and self._shake_ui_known
            and not self._stall_shake_seen
            and waited >= STALL_NO_SHAKE_MS
        ):
            self.recover(f"stall guard: no shake prompt {waited} ms after cast")
            return True
        return False

    def _finish_catch(self) -> None:
        """The shared tail of every reeling phase: score it and end the cycle."""
        s = self.state
        if not s.outcome_resolved:
            s.outcome_resolved = True
            if s.completion_reached:
                s.fish_caught_count += 1
                if self.recover_level:
                    log.info("recover: fish landed, back to normal after L%d", self.recover_level)
                    self.recover_level = 0
                if self.stellarwave.seen_this_catch:
                    self.center_cursor()
                if s.reel_progress_peak is not None and not s.reel_progress_dropped:
                    s.perfect_count += 1
            else:
                s.fish_lost_count += 1
            if s.cycle_started_at:
                # Time the whole cast->catch cycle, not just the reel: dead time
                # between casts counts against throughput just as much.
                s.cycle_times.append((now_ms() - s.cycle_started_at) / 1000.0)
                del s.cycle_times[:-30]  # rolling window, so tuning shows up fast
            # The announcement lands a beat after the reel GUI goes; watch for it
            # over the next couple of seconds rather than sampling once here.
            self.catches.begin_poll()
        self.stop_macro_cycle("DONE")

    def _track_progress(self, progress) -> None:
        """Note whether the reel's progress ever went backwards.

        That is what separates a perfect catch from an ordinary one: the fish
        never slipped out of the bar long enough to cost progress. A small
        tolerance absorbs read jitter between two frames of the same value.
        """
        if progress is None:
            return
        s = self.state
        if s.reel_progress_peak is None or progress > s.reel_progress_peak:
            s.reel_progress_peak = progress
        elif progress < s.reel_progress_peak - 0.75:
            s.reel_progress_dropped = True

    # -- throughput ------------------------------------------------------

    def fish_per_hour(self) -> float:
        s = self.state
        if not s.session_started_at:
            return 0.0
        hours = (now_ms() - s.session_started_at) / 3_600_000.0
        return (s.fish_caught_count / hours) if hours > 0 else 0.0

    def average_cycle_seconds(self) -> float:
        s = self.state
        return sum(s.cycle_times) / len(s.cycle_times) if s.cycle_times else 0.0

    def update_fishing_phase(self) -> None:
        s = self.state
        s.power_percent = ""

        reel_visible = self.game.is_reel_gui_visible()
        ctx = self.game.get_reel_bar_context() if reel_visible else None

        progress = self.game.get_fishing_completion_percent()
        s.progress_percent = "" if progress is None else round(progress)
        self._track_progress(progress)

        if progress is not None and progress >= float(self.main["completion_threshold"]):
            s.completion_reached = True

        if s.completion_reached:
            self.release_mouse(force=True)
            self.controller.reset()
            if reel_visible and self.main.get("early_cast"):
                # The finished reel lingers ~1 s before the game removes it.
                # Cast through that instead of waiting it out; the old reel is
                # remembered so the next cycle never mistakes it for a new bite.
                self.game.stale_reel_gui = self.game.get_reel_gui()
                s.early_cast_pending = True
                self._finish_catch()
                return
            if reel_visible:
                s.fishing_lost_at = 0
                return
            ctx = None

        if ctx:
            s.fishing_lost_at = 0
            if not self.assists(self.reel_part()):
                return
            if self.main.get("stellarwave_auto_signs") and self.stellarwave.update(ctx):
                return
            if self.game.has_active_fishing_context(ctx):
                self.controller.update(ctx)
            else:
                self.release_mouse()
            return

        self.release_mouse()
        self.controller.reset()
        if not s.fishing_lost_at:
            s.fishing_lost_at = now_ms()
        if (now_ms() - s.fishing_lost_at) >= s.fishing_end_grace_ms:
            self._finish_catch()

    def update_bellona_phase(self) -> None:
        s = self.state
        s.power_percent = ""
        threshold = float(self.main["completion_threshold"])
        contexts = self.game.get_reel_contexts()

        self.controller.update_completion_state(threshold)
        progress = self.controller.get_progress_percent()
        s.progress_percent = "" if progress is None else round(progress)
        self._track_progress(progress)

        if s.completion_reached:
            self.release_all_fishing_mouse(force=True)
            self.controller.reset()
            if contexts:
                s.fishing_lost_at = 0
                return
        elif contexts and self.controller.has_active_context():
            s.fishing_lost_at = 0
            if self.assists("minigames"):
                self.controller.update()
            return

        self.release_all_fishing_mouse()
        self.controller.reset()
        if not s.fishing_lost_at:
            s.fishing_lost_at = now_ms()
        if (now_ms() - s.fishing_lost_at) >= s.fishing_end_grace_ms:
            self._finish_catch()

    def update_tranquility_phase(self) -> None:
        s = self.state
        s.power_percent = ""
        root = self.game.get_tranquility_root()
        progress = self.game.read_tranquility_progress(root)
        s.progress_percent = "" if progress is None else round(progress)
        self._track_progress(progress)

        if progress is not None and progress >= float(self.main["completion_threshold"]):
            s.completion_reached = True

        container = self.game.get_tranquility_lane_container(root) if root else 0
        if container:
            s.fishing_lost_at = 0
            if self.assists("minigames"):
                self.controller.update()
            return

        if not s.fishing_lost_at:
            s.fishing_lost_at = now_ms()
        if (now_ms() - s.fishing_lost_at) >= s.fishing_end_grace_ms:
            # The rod is still wrapping up the catch when the lanes go, and a
            # cast pressed now is swallowed. Wait for the rod to read ready.
            s.early_cast_pending = True
            self._finish_catch()

    def update_lullaby_phase(self) -> None:
        """A metronome replaces the reel bar; progress still comes from the reel."""
        s = self.state
        s.power_percent = ""
        metronome_active = self.game.is_metronome_active()

        progress = self.game.get_fishing_completion_percent()
        s.progress_percent = "" if progress is None else round(progress)
        self._track_progress(progress)
        if progress is not None and progress >= float(self.main["completion_threshold"]):
            s.completion_reached = True

        if s.completion_reached:
            self.controller.reset()
            if metronome_active:
                s.fishing_lost_at = 0
                return
        elif metronome_active:
            s.fishing_lost_at = 0
            if self.assists("minigames"):
                self.controller.update()
            return

        self.controller.reset()
        if not s.fishing_lost_at:
            s.fishing_lost_at = now_ms()
        if (now_ms() - s.fishing_lost_at) >= s.fishing_end_grace_ms:
            self._finish_catch()

    # -- auto totem -------------------------------------------------------

    def is_auto_totem_enabled(self) -> bool:
        return bool(
            self.main["auto_totem_enabled"]
            and self.main["auto_totem_name"] == "Aurora Totem"
        )

    def is_public_server_enabled(self) -> bool:
        return bool(self.main["public_server_enabled"])

    def auto_totem_interval_ms(self) -> int:
        return max(1, int(self.main["auto_totem_interval_sec"])) * 1000

    def reset_auto_totem_control(self) -> None:
        s = self.state
        s.totem_state = "IDLE"
        s.totem_retry_count = 0
        s.totem_wait_started_at = 0
        s.totem_pending = False
        s.totem_blocked_until_catch_end = False
        s.totem_needs_rod_reequip = False
        s.totem_needs_settle_delay = False

    def is_auto_totem_boundary(self) -> bool:
        """Only swap items between casts, never mid-reel."""
        s = self.state
        return s.phase == "CASTING" and not s.is_holding and not s.cast_bar_seen

    def is_auto_totem_due(self) -> bool:
        s = self.state
        if not self.is_auto_totem_enabled():
            return False
        if self.main["auto_totem_mode"] == "interval":
            reference = max(s.last_totem_success_at, s.last_totem_attempt_at)
            return not reference or (now_ms() - reference) >= self.auto_totem_interval_ms()
        if s.totem_night_covered:
            cycle = self.game.get_current_cycle().lower()
            if not cycle or "night" in cycle:
                return False
            s.totem_night_covered = False
        return True

    def cancel_totem(self) -> None:
        s = self.state
        needs_reequip = s.totem_needs_rod_reequip
        self.reset_auto_totem_control()
        s.last_totem_attempt_at = now_ms()
        s.totem_blocked_until_catch_end = True
        if needs_reequip:
            self.ensure_rod_equipped()

    def update_auto_totem(self) -> bool:
        """Returns True when the totem workflow owns this tick."""
        s = self.state
        if not self.is_auto_totem_enabled():
            if s.totem_state != "IDLE" or s.totem_pending or s.totem_blocked_until_catch_end:
                self.release_mouse()
                self.controller.reset()
                if s.totem_state != "IDLE" and s.totem_needs_rod_reequip:
                    self.select_hotbar_slot("1")
                self.reset_auto_totem_control()
            return False

        if self.is_public_server_enabled() and self.game.is_totem_blocked():
            if s.totem_state != "IDLE" or s.totem_pending:
                self.cancel_totem()
            return False

        if s.totem_state != "IDLE":
            s.power_percent = ""
            s.progress_percent = ""
            self.release_mouse()
            self.controller.reset()
            self.update_auto_totem_state()
            return True

        if not s.cycle_enabled:
            return False

        if s.totem_pending and self.is_auto_totem_boundary():
            self.begin_auto_totem_workflow()
            return True

        if s.totem_blocked_until_catch_end:
            return False

        if self.is_auto_totem_due():
            if self.is_auto_totem_boundary():
                self.begin_auto_totem_workflow()
                return True
            if not s.totem_pending and s.phase != "OFF":
                s.totem_needs_settle_delay = True
            s.totem_pending = True
        return False

    def begin_auto_totem_workflow(self) -> None:
        s = self.state
        s.power_percent = ""
        s.progress_percent = ""
        s.totem_pending = False
        s.totem_retry_count = 0
        s.totem_wait_started_at = 0
        s.last_totem_attempt_at = now_ms()
        s.totem_needs_rod_reequip = False

        self.release_mouse()
        self.controller.reset()
        if s.totem_needs_settle_delay:
            s.totem_state = "TOTEM_SETTLE"
            s.totem_wait_started_at = now_ms()
            return
        self.run_auto_totem_step()

    def try_use_hotbar_item(self, item_name: str) -> bool:
        """TryUseHotbarItem(): select the slot, click, confirm it equipped."""
        slot_key = self.game.get_hotbar_item_slot_key(item_name)
        if not slot_key:
            return False
        for _ in range(2):
            before = self.game.get_equipped_tool_name()
            if before != item_name:
                if not self.select_hotbar_slot(slot_key):
                    return False
                time.sleep(0.175)
            self.sender.click()
            time.sleep(0.1)
            after = self.game.get_equipped_tool_name()
            if after == item_name or before == item_name:
                return True
            time.sleep(0.125)
        return False

    def try_use_totem(self, item_name: str) -> bool:
        if not self.try_use_hotbar_item(item_name):
            return False
        self.state.totem_needs_rod_reequip = True
        return True

    def run_auto_totem_step(self) -> None:
        s = self.state
        if self.is_public_server_enabled() and self.game.is_totem_blocked():
            self.cancel_totem()
            return
        if self.game.is_aurora_active():
            self.complete_auto_totem_workflow(True)
            return
        if self.game.is_night_cycle():
            if not self.try_use_totem("Aurora Totem"):
                self.complete_auto_totem_workflow(False)
                return
            s.totem_state = "TOTEM_WAIT_AURORA"
            s.totem_wait_started_at = now_ms()
            return
        # Daytime: force night first with a Sundial, then pop the Aurora.
        if not self.try_use_totem("Sundial Totem"):
            self.complete_auto_totem_workflow(False)
            return
        s.totem_state = "TOTEM_WAIT_NIGHT"
        s.totem_wait_started_at = now_ms()

    def update_auto_totem_state(self) -> None:
        s = self.state
        if self.is_public_server_enabled() and self.game.is_totem_blocked():
            self.cancel_totem()
            return
        if self.game.is_aurora_active():
            self.complete_auto_totem_workflow(True)
            return

        if s.totem_state == "TOTEM_SETTLE":
            if (now_ms() - s.totem_wait_started_at) < self.cycle_start_delay_ms():
                return
            s.totem_needs_settle_delay = False
            s.totem_wait_started_at = 0
            self.run_auto_totem_step()
            return

        if s.totem_state == "TOTEM_WAIT_NIGHT":
            if self.game.is_night_cycle():
                s.totem_retry_count = 0
                if not self.try_use_totem("Aurora Totem"):
                    self.complete_auto_totem_workflow(False)
                    return
                s.totem_state = "TOTEM_WAIT_AURORA"
                s.totem_wait_started_at = now_ms()
                return
            if (now_ms() - s.totem_wait_started_at) < AUTO_TOTEM_WAIT_MS:
                return
            if s.totem_retry_count >= 1:
                self.complete_auto_totem_workflow(False)
                return
            if not self.try_use_totem("Sundial Totem"):
                self.complete_auto_totem_workflow(False)
                return
            s.totem_retry_count += 1
            s.totem_wait_started_at = now_ms()
            return

        if s.totem_state == "TOTEM_WAIT_AURORA":
            if (now_ms() - s.totem_wait_started_at) < AUTO_TOTEM_WAIT_MS:
                return
            if s.totem_retry_count >= 1:
                self.complete_auto_totem_workflow(False)
                return
            if not self.try_use_totem("Aurora Totem"):
                self.complete_auto_totem_workflow(False)
                return
            s.totem_retry_count += 1
            s.totem_wait_started_at = now_ms()

    def complete_auto_totem_workflow(self, success: bool = False) -> None:
        s = self.state
        needs_reequip = s.totem_needs_rod_reequip
        if success:
            s.last_totem_success_at = now_ms()
            s.totem_night_covered = True
            s.totem_pop_count += 1
        elif self.webhook_cfg["webhook_alert_totem_failed"] and self.webhook:
            self.webhook.send_alert(
                "Auto Totem Failed",
                "The auto totem workflow could not complete successfully.",
            )

        self.reset_auto_totem_control()
        if needs_reequip:
            self.ensure_rod_equipped()
        if not success and self.main["auto_totem_mode"] == "expire":
            s.totem_blocked_until_catch_end = True
        if s.cycle_enabled and s.phase == "CASTING":
            self.initialize_cast_cycle()

    # -- Stellarwave signs -----------------------------------------------

    def stellarwave_screen_point(self, x: float, y: float) -> tuple[int, int]:
        """A sign's AbsolutePosition centre, plus the user's fine-tune offset.

        Still in Roblox GUI units; inputs/window.py maps it onto the screen
        (window origin, title bar, top-bar inset, Retina scale).
        """
        u = self.userprefs
        try:
            dx = int(float(u.get("stellarwave_offset_x", 0) or 0))
            dy = int(float(u.get("stellarwave_offset_y", 0) or 0))
        except ValueError:
            dx = dy = 0
        return round(x) + dx, round(y) + dy

    def center_cursor(self) -> None:
        """Park the cursor mid-window after a Stellarwave catch."""
        if wincursor.center_cursor(self):
            return
        width, height = self.screen_size
        self.sender.move_absolute(width // 2, height // 2, width, height)

    # -- appraisal --------------------------------------------------------

    def is_auto_appraise_enabled(self) -> bool:
        return bool(self.main.get("auto_appraise_enabled"))

    def appraise_click_point(self):
        """A configured screen point, or None to click wherever the cursor is.

        Fisch's current appraisal UI keeps its button in one place, so parking
        the cursor over it once is enough and the macro can simply click. The
        coordinate keys are still honoured if set -- they cost nothing to keep
        and a saved Windows settings.json may carry them -- but nothing requires
        them any more.
        """
        u = self.userprefs
        x, y = str(u.get("auto_appraise_click_x", "")), str(u.get("auto_appraise_click_y", ""))
        if x.strip() and y.strip():
            try:
                return int(float(x)), int(float(y))
            except ValueError:
                return None
        return None

    def clear_appraise_cache(self) -> None:
        s = self.state
        s.appraise_state = "IDLE"
        s.appraise_last_click_at = 0
        s.appraise_wait_started_at = 0
        s.appraise_retry_count = 0
        s.appraise_last_retry_at = 0
        s.appraise_click_count = 0
        s.appraise_coins_before = None
        s.appraise_sig_before = None
        s.appraise_start_coins = None
        s.appraise_end_coins = None
        s.appraise_last_error = ""

    def set_appraise_status(self, message: str) -> None:
        self.state.appraise_status = message

    def click_appraise_point(self) -> None:
        """Click the appraise button: in place, unless a point is configured."""
        point = self.appraise_click_point()
        if point is None:
            self.sender.click()
            return
        width, height = self.screen_size
        if not self.sender.click_absolute(point[0], point[1], width, height):
            self.sender.click()  # backend has no absolute pointer -- click in place

    def start_appraise_cycle(self) -> tuple[bool, str]:
        s = self.state
        if not self.game.is_anything_equipped():
            return False, "You have to have a fish selected when appraising."
        desired = str(self.main["auto_appraise_mutation"]).strip()
        if not desired:
            return False, "Choose a desired mutation before starting."

        self.release_mouse(force=True)
        self.clear_appraise_cache()
        s.phase = "APPRAISE"
        s.appraise_state = "RESOLVING"
        s.cycle_enabled = True
        self.set_appraise_status("Resolving fish info...")

        subvalues = self.game.resolve_fish_info_subvalues()
        if not subvalues:
            self.fail_appraise_cycle(
                "Could not find fishinfo/Info/Subvalues. Hold the fish before appraising."
            )
            return False, s.appraise_last_error

        s.appraise_start_coins = self.game.read_current_coins()
        if self.has_desired_mutation(desired):
            s.cycle_enabled = False
            s.phase = "DONE"
            s.appraise_state = "DONE"
            self.set_appraise_status(f"{desired} mutation was already present.")
            return True, "Already present."

        s.appraise_state = "CLICK"
        self.set_appraise_status("Ready.")
        return True, "Started."

    def stop_appraise_cycle(self, next_phase: str = "OFF", status: str = "Stopped.") -> None:
        s = self.state
        self.release_mouse(force=True)
        s.cycle_enabled = False
        s.phase = next_phase
        if next_phase == "OFF":
            self.clear_appraise_cache()
        else:
            s.appraise_state = next_phase
        self.set_appraise_status(status)

    def appraise_signature(self):
        """(Subvalues address, its text) -- changes when the fish info is rebuilt.

        The game tears down and recreates the Subvalues subtree when an appraisal
        resolves, so both the pointer and the text are useful change signals.
        Returns None if the info is momentarily unavailable.
        """
        try:
            subvalues = self.game.resolve_fish_info_subvalues()
            if not subvalues:
                return None
            return (subvalues, self.game.collect_subvalues_text(subvalues))
        except Exception:
            return None

    def appraise_resolved(self) -> bool:
        """Has the appraisal actually completed since the last click?

        Two independent signals, either of which is sufficient:

        * Coins went down. Appraising is charged, so a decrease means the game
          processed it. This is the reliable one -- it fires even when the reroll
          happens to produce the exact same mutations as before.
        * The Subvalues subtree changed (address or text). Catches the case where
          the coin readout is unavailable or unchanged.

        Requiring one of these before evaluating is what stops the loop spending
        a second appraisal on top of an unresolved first.
        """
        s = self.state
        coins_now = self.game.read_current_coins()
        if (
            coins_now is not None
            and s.appraise_coins_before is not None
            and coins_now < s.appraise_coins_before
        ):
            return True
        sig_now = self.appraise_signature()
        if sig_now is None:
            return False
        return sig_now != s.appraise_sig_before

    def has_desired_mutation(self, desired: str) -> bool:
        subvalues = self.game.resolve_fish_info_subvalues()
        if not subvalues:
            raise RuntimeError("Subvalues unavailable; re-equip the fish.")
        desired = desired.strip().lower()
        if not desired:
            return False
        haystack = self.game.collect_subvalues_text(subvalues).lower()
        return desired in haystack

    def update_appraise_phase(self) -> None:
        """Autoclick the appraise button, checking memory between clicks.

        The original ran a four-state sequence (click 1/2, click 2/2, wait,
        retry) because the old appraisal UI needed a menu opened and a result
        confirmed at two different spots. The current UI keeps everything in one
        place, so the whole thing collapses to: click, wait, look. Detection is
        unchanged -- still the Subvalues read, never pixels.
        """
        s = self.state
        desired = str(self.main["auto_appraise_mutation"]).strip()
        delay_ms = int(self.main["appraise_delay_ms"])

        if s.appraise_state == "CLICK":
            # Snapshot before clicking so the wait below can tell "the appraisal
            # landed" from "nothing has happened yet".
            s.appraise_coins_before = self.game.read_current_coins()
            s.appraise_sig_before = self.appraise_signature()
            self.click_appraise_point()
            s.appraise_last_click_at = now_ms()
            s.appraise_click_count += 1
            self.set_appraise_status(f"Appraising... ({s.appraise_click_count} clicks)")
            s.appraise_state = "CHECK"
            return

        if s.appraise_state == "CHECK":
            elapsed = now_ms() - s.appraise_last_click_at
            if elapsed < delay_ms:
                return
            # Appraising is not instant. Reading on a timer alone would evaluate
            # the pre-appraisal values, conclude "not found", and click again --
            # spending another appraisal before the previous roll had resolved,
            # and potentially rolling past the mutation without ever seeing it.
            # So wait for the game itself to say the roll happened.
            if not self.appraise_resolved():
                if elapsed < APPRAISE_RESOLVE_TIMEOUT_MS:
                    self.set_appraise_status(
                        f"Waiting for appraisal to resolve... "
                        f"({s.appraise_click_count} clicks)"
                    )
                    return
                # Nothing changed in the timeout window: the click probably
                # missed the button. Try again rather than stall.
                self.set_appraise_status("No response -- clicking again.")
                s.appraise_state = "CLICK"
                return
            try:
                if self.has_desired_mutation(desired):
                    self.complete_appraise_cycle(
                        f"Found {desired} after {s.appraise_click_count} clicks."
                    )
                    return
            except RuntimeError as exc:
                # Subvalues briefly vanish while the game rebuilds the fish info,
                # so a miss here is usually a race rather than a real failure.
                if s.appraise_last_retry_at and \
                   (now_ms() - s.appraise_last_retry_at) < APPRAISE_FIXED_RETRY_MS:
                    return
                s.appraise_last_retry_at = now_ms()
                s.appraise_retry_count += 1
                if s.appraise_retry_count <= APPRAISE_SUBVALUES_MAX_RETRIES:
                    self.set_appraise_status(
                        f"Waiting for fish info... "
                        f"{s.appraise_retry_count}/{APPRAISE_SUBVALUES_MAX_RETRIES}"
                    )
                    return
                self.fail_appraise_cycle(str(exc))
                return

            s.appraise_retry_count = 0
            s.appraise_last_retry_at = 0
            s.appraise_state = "CLICK"

    def complete_appraise_cycle(self, status: str) -> None:
        s = self.state
        s.appraise_end_coins = self.game.read_current_coins()
        s.cycle_enabled = False
        s.appraise_state = "DONE"
        s.phase = "DONE"
        self.set_appraise_status(status)
        if self.webhook:
            self.webhook.send_appraise_result(self, True, status)

    def fail_appraise_cycle(self, message: str) -> None:
        s = self.state
        s.appraise_end_coins = self.game.read_current_coins()
        s.cycle_enabled = False
        s.appraise_state = "FAILED"
        s.appraise_last_error = message
        s.phase = "FAILED"
        self.set_appraise_status(message)
        if self.webhook:
            self.webhook.send_appraise_result(self, False, message)

    # -- rod watching -----------------------------------------------------

    def refresh_rod(self, force: bool = False) -> None:
        """Keep the equipped-rod label live; commit only a settled hotbar.

        The rod streams in a beat after the PlaceId flips to Fisch, and reading
        it early yields the wrong rod -- which then picks the wrong controller.
        So the settle clock starts when the hotbar first shows an item, not when
        the place changes.
        """
        if not self.link.is_memory_ready() or not self.link.is_in_fisch_game():
            self._hotbar_init_at = 0
            return

        if self.rod and not force:
            # A rod swap is a deliberate act that takes seconds; re-walking the
            # hotbar on every 20 ms tick bought nothing but syscalls.
            if now_ms() - self._rod_checked_at < ROD_RECHECK_MS:
                return
            self._rod_checked_at = now_ms()
            rod = self.game.get_hotbar_rod_name()
            if rod and rod != self.rod:
                self.rod = rod
            return

        if not self.game.is_hotbar_populated():
            self._hotbar_init_at = 0
            return
        if not self._hotbar_init_at:
            self._hotbar_init_at = now_ms()
        if force or (now_ms() - self._hotbar_init_at) >= 3000:
            rod = self.game.get_hotbar_rod_name()
            if rod:
                self.rod = rod
