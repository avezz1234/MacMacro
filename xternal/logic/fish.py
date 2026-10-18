"""
XTernal for Mac -- fishing state machine and rod controllers.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of shared/Fish.ahk, plus the totem workflow from shared/Totem.ahk and the
appraisal loop from shared/Appraise.ahk.

This is the part of the macro with no platform dependency at all: the phase
machine, the PID tracking and the per-rod behaviour are reproduced as they are,
because they encode how the game actually behaves. Only the two edges change --
reads go through FischGame, and input goes through InputSender.
"""

from __future__ import annotations

import time

from ..settings import MIN_CAST_TIMEOUT_MS
from .game import (
    FischGame,
    is_bellona_rod,
    is_dreambreaker_rod,
    is_lullaby_rod,
    is_pinion_rod,
    is_requiem_rod,
    is_tranquility_rod,
    now_ms,
)

#: The Requiem rod tracks poorly below this; forced for the session while it is
#: equipped and never written back to the user's saved settings.
REQUIEM_ACTION_DELAY_MS = 165

#: How long to let the hotbar settle before trusting the rod name.
ROD_READ_DELAY_MS = 3000
ROD_WATCH_INTERVAL_MS = 3000
ATTACH_WATCHER_INTERVAL_MS = 1000

AUTO_TOTEM_WAIT_MS = 30000
APPRAISE_FIXED_RETRY_MS = 500
APPRAISE_SUBVALUES_MAX_RETRIES = 5

#: Longest to wait for an appraisal to actually resolve after a click. Past this
#: the click is assumed not to have registered (misplaced cursor, menu closed)
#: and the loop clicks again rather than hanging forever.
APPRAISE_RESOLVE_TIMEOUT_MS = 6000


class MacroState:
    """CreateFishingMacro(): all mutable per-session state in one object."""

    def __init__(self):
        self.phase = "OFF"
        self.power_percent = ""
        self.progress_percent = ""
        self.is_holding = False
        self.is_holding_right = False
        self.last_right_action_at = 0
        self.bellona_left_done = False
        self.bellona_right_done = False
        self.cast_threshold = 96.0
        self.cast_wait_timeout_ms = 15000
        self.fishing_end_grace_ms = 100
        self.cast_started_at = 0
        self.cast_released_at = 0
        self.cast_bar_seen = False
        self.fishing_lost_at = 0
        self.completion_reached = False
        self.outcome_resolved = False
        self.fish_caught_count = 0
        self.fish_lost_count = 0
        self.cast_timeout_count = 0
        self.totem_pop_count = 0
        self.shaking_interval_ms = 25
        self.last_shaked_at = 0
        self.last_action_at = 0
        self.activated_ui_nav = False
        self.cycle_enabled = False
        #: Set when main.early_cast started this cast over a lingering reel.
        self.early_cast_pending = False
        # totem
        self.totem_state = "IDLE"
        self.totem_retry_count = 0
        self.totem_wait_started_at = 0
        self.last_totem_success_at = 0
        self.last_totem_attempt_at = 0
        self.totem_pending = False
        self.totem_blocked_until_catch_end = False
        self.totem_night_covered = False
        self.totem_needs_rod_reequip = False
        self.totem_needs_settle_delay = False
        # appraise
        self.appraise_state = "IDLE"
        self.appraise_last_click_at = 0
        self.appraise_wait_started_at = 0
        self.appraise_start_coins = None
        self.appraise_end_coins = None
        self.appraise_last_error = ""
        self.appraise_retry_count = 0
        self.appraise_last_retry_at = 0
        self.appraise_click_count = 0
        self.appraise_status = ""
        # Snapshot taken at click time, used to tell a resolved appraisal from a
        # stale read. Checking on a timer alone re-clicks before the roll lands.
        self.appraise_coins_before = None
        self.appraise_sig_before = None
        # Throughput accounting. Fish per hour is the number that actually
        # answers "is this setting better?", and it cannot be eyeballed from a
        # catch counter -- a change that speeds up reeling but costs you catches
        # looks identical to one that helps until you divide by time.
        self.session_started_at = 0
        self.cycle_started_at = 0
        self.cycle_times: list[float] = []
        # Perfect catches: landed without the progress bar ever going back.
        self.reel_progress_peak = None
        self.reel_progress_dropped = False
        self.perfect_count = 0


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


class FishingController:
    """The default single-reel controller: a PID loop driven by PWM clicking.

    `button` defaults to LButton so every ordinary rod behaves as before;
    Bellona builds a left and a right instance to drive its two reels.
    """

    def __init__(self, engine, button: str = "LButton"):
        self.engine = engine
        self.button = button
        self.last_playerbar_pos = None
        self.last_fish_pos = None
        self.pwm_accumulator = None

    def reset(self) -> None:
        self.last_playerbar_pos = None
        self.last_fish_pos = None
        self.pwm_accumulator = None

    # -- geometry ---------------------------------------------------------

    def get_fish_position(self, ctx):
        parts = self.engine.game.indicator_positions(ctx)
        return parts[0] if parts else None

    def get_playerbar_position(self, ctx):
        parts = self.engine.game.indicator_positions(ctx)
        return parts[1] if parts else None

    # -- inversion (Dreambreaker) ----------------------------------------

    def is_inverted(self) -> bool:
        """Dreambreaker inverts the controls past 40% progress."""
        if not self.engine.dreambreaker:
            return False
        progress = self.engine.game.get_fishing_completion_percent()
        if progress is None:
            return False
        return progress >= 40.0

    def hold(self) -> None:
        if self.button == "RButton":
            if self.is_inverted():
                self.engine.release_right_mouse()
            else:
                self.engine.hold_right_mouse()
            return
        if self.is_inverted():
            self.engine.release_mouse()
        else:
            self.engine.hold_mouse()

    def release(self) -> None:
        if self.button == "RButton":
            if self.is_inverted():
                self.engine.hold_right_mouse()
            else:
                self.engine.release_right_mouse()
            return
        if self.is_inverted():
            self.engine.hold_mouse()
        else:
            self.engine.release_mouse()

    # -- the loop ---------------------------------------------------------

    def update(self, ctx=None) -> None:
        game = self.engine.game
        main = self.engine.main
        if ctx is None:
            ctx = game.get_reel_bar_context()

        is_safe = game.is_indicator_safe(ctx)
        if is_safe is None:
            self.release()
            return

        fish_pos = self.get_fish_position(ctx)
        playerbar_pos = self.get_playerbar_position(ctx)
        if fish_pos is None or playerbar_pos is None:
            return

        if self.last_playerbar_pos is None:
            self.last_playerbar_pos = playerbar_pos
        if self.last_fish_pos is None:
            self.last_fish_pos = fish_pos

        playerbar_velocity = playerbar_pos - self.last_playerbar_pos
        self.last_playerbar_pos = playerbar_pos
        fish_velocity = fish_pos - self.last_fish_pos
        self.last_fish_pos = fish_pos

        error = fish_pos - playerbar_pos

        edge = float(main["edge_boundary"])
        if playerbar_pos < edge:
            self.hold()
            return
        if playerbar_pos > 1 - edge:
            self.release()
            return

        predicted = playerbar_pos + (playerbar_velocity * float(main["prediction_strength"]))
        predicted_error = fish_pos - predicted

        close_threshold = float(main["close_threshold"])
        same_side_after_prediction = (error * predicted_error) > 0
        approaching = (error * playerbar_velocity) > 0
        remaining = max(0.0, abs(error) - close_threshold)

        # Stop steering and start bleeding speed before overshooting.
        brake_lookahead = abs(playerbar_velocity) * 8
        needs_pre_slow = approaching and (brake_lookahead >= remaining)

        # Full-deflection correction only while far out and not yet braking.
        if abs(error) > close_threshold and same_side_after_prediction and not needs_pre_slow:
            if error > 0:
                self.hold()
            else:
                self.release()
            return

        neutral = float(main["neutral_duty_cycle"])

        if needs_pre_slow and brake_lookahead > 0:
            urgency = 1.0 - min(1.0, remaining / brake_lookahead)
            if error > 0:
                target_duty = neutral * (1.0 - urgency)
            else:
                target_duty = neutral + ((1.0 - neutral) * urgency)
        else:
            kp = float(main["proportional_gain"])
            kd = float(main["derivative_gain"])
            kv = float(main["velocity_damping"])
            adjustment = (kp * error) + (kd * fish_velocity) - (kv * playerbar_velocity)
            target_duty = max(0.0, min(1.0, neutral + adjustment))

        if self.pwm_accumulator is None:
            self.pwm_accumulator = 0.0
        self.pwm_accumulator += target_duty
        if self.pwm_accumulator >= 1.0:
            self.pwm_accumulator -= 1.0
            self.hold()
        else:
            self.release()


class RequiemController(FishingController):
    """Reels like any single-reel rod; only its forced action delay differs."""


class PinionController(FishingController):
    """Pinion's Aria: notes must be caught alongside the fish.

    Seven consecutive caught notes trigger "resonance", after which the note
    itself becomes the only target.
    """

    NOTE_DEADZONE = -16.5

    def __init__(self, engine, button: str = "LButton"):
        super().__init__(engine, button)
        self.notes_caught = 0
        self.note_counted = False
        self.resonance_active = False

    def reset(self) -> None:
        super().reset()
        self.notes_caught = 0
        self.note_counted = False
        self.resonance_active = False

    def get_both_targets(self, fish_x, note_x, half_width):
        """A position covering both fish and note, or None if they're too far apart."""
        distance = abs(note_x - fish_x)
        if distance > half_width * 2:
            return None
        if distance <= half_width:
            return fish_x
        return note_x - half_width if note_x > fish_x else note_x + half_width

    def get_note_deadzone(self, playerbar_x, fish_x, note_x):
        to_note = abs(note_x - playerbar_x)
        fish_to_note = abs(note_x - fish_x)
        dz = self.NOTE_DEADZONE - (to_note * 30.0) - (fish_to_note * 10.0)
        return max(-22, min(self.NOTE_DEADZONE, dz))

    def update_note_count(self, note, ctx) -> None:
        if not self.note_counted and -0.8 <= note["sy"] <= 0.53:
            if self.engine.game.is_note_in_playerbar(note["sx"], ctx, 0.1):
                self.note_counted = True
                self.notes_caught += 1
            else:
                self.notes_caught = 0
                self.resonance_active = False
                self.note_counted = True
        if note["sy"] < -8:
            self.note_counted = False
        if self.notes_caught >= 7:
            self.resonance_active = True

    def get_fish_position(self, ctx):
        game = self.engine.game
        fish_x = super().get_fish_position(ctx)
        if not ctx or not ctx.get("playerbar"):
            return fish_x
        half_width = game.r.frame_scale_width(ctx["playerbar"]) / 2.0
        playerbar_x = self.get_playerbar_position(ctx)
        if playerbar_x is None:
            return fish_x

        note = game.get_active_note_target()
        if note is None:
            return fish_x
        if self.resonance_active:
            return note["sx"]

        self.update_note_count(note, ctx)

        deadzone = self.get_note_deadzone(playerbar_x, fish_x, note["sx"])
        if note["sy"] <= deadzone:
            return fish_x

        both = self.get_both_targets(fish_x, note["sx"], half_width)
        if both is not None:
            return both
        return note["sx"]


class BellonaController:
    """Bellona's Waraxe drives two reels at once: left on LMB, right on RMB.

    Reuses the base PID per side and only reports a catch once both sides pass
    the completion threshold.
    """

    def __init__(self, engine):
        self.engine = engine
        self.left = FishingController(engine, "LButton")
        self.right = FishingController(engine, "RButton")

    def reset(self) -> None:
        self.left.reset()
        self.right.reset()
        self.engine.release_all_fishing_mouse(force=True)

    def get_contexts(self):
        return self.engine.game.get_reel_contexts()

    def get_side_contexts(self):
        contexts = self.get_contexts()
        left = right = None
        if len(contexts) >= 2:
            left, right = contexts[0], contexts[-1]
        elif len(contexts) == 1:
            if contexts[0]["barX"] < 0.5:
                left = contexts[0]
            else:
                right = contexts[0]
        return left, right

    def has_active_context(self) -> bool:
        return any(c.get("fish") and c.get("playerbar") for c in self.get_contexts())

    def get_progress_percent(self):
        values = []
        for ctx in self.get_contexts():
            progress = self.engine.game.read_reel_completion_percent(ctx)
            if progress is not None:
                values.append(progress)
        return min(values) if values else None

    def update_completion_state(self, threshold: float) -> None:
        state = self.engine.state
        left, right = self.get_side_contexts()
        if left:
            progress = self.engine.game.read_reel_completion_percent(left)
            if progress is not None and progress >= threshold:
                state.bellona_left_done = True
        if right:
            progress = self.engine.game.read_reel_completion_percent(right)
            if progress is not None and progress >= threshold:
                state.bellona_right_done = True
        state.completion_reached = state.bellona_left_done and state.bellona_right_done

    def update(self, ctx=None) -> None:
        left, right = self.get_side_contexts()
        if left and left.get("fish") and left.get("playerbar"):
            self.left.update(left)
        else:
            self.left.reset()
            self.engine.release_mouse(force=True)
        if right and right.get("fish") and right.get("playerbar"):
            self.right.update(right)
        else:
            self.right.reset()
            self.engine.release_right_mouse(force=True)


class TranquilityController:
    """Tranquility Rod: a four-lane rhythm game played on the keyboard."""

    HIT_Y_MIN = 0.78
    HIT_Y_MAX = 0.90
    KEY_COOLDOWN_MS = 30

    def __init__(self, engine):
        self.engine = engine
        self.hit_notes = {}
        self.last_key_sent_at = {}

    def reset(self) -> None:
        self.engine.release_mouse(force=True)
        self.hit_notes = {}
        self.last_key_sent_at = {}

    def press_lane_key(self, key: str, note_addr: int) -> bool:
        now = now_ms()
        last = self.last_key_sent_at.get(key, 0)
        if last and (now - last) < self.KEY_COOLDOWN_MS:
            return False
        self.engine.sender.send("{" + key + "}")
        self.last_key_sent_at[key] = now
        self.hit_notes[note_addr] = now
        return True

    def update(self, ctx=None) -> None:
        game = self.engine.game
        self.engine.release_mouse(force=True)

        root = game.get_tranquility_root()
        if not root:
            return
        container = game.get_tranquility_lane_container(root)
        if not container:
            return

        seen = set()
        for index in range(1, 5):
            lane = game.get_tranquility_lane(index, container)
            if not lane or not game.r.read_gui_visible(lane):
                continue
            key = game.get_tranquility_lane_key(index, root, lane)
            if not key:
                continue
            for note in game.r.read_children(lane):
                if game.r.read_instance_name(note) != "Note":
                    continue
                if game.r.read_class_name(note) != "ImageLabel":
                    continue
                seen.add(note)
                if note in self.hit_notes or not game.r.read_gui_visible(note):
                    continue
                pos = game.r.note_position(note)
                if not (-5.0 < pos["sy"] < 5.0):  # IsReasonableGuiScale
                    continue
                if self.HIT_Y_MIN <= pos["sy"] <= self.HIT_Y_MAX:
                    self.press_lane_key(key, note)

        for note in [n for n in self.hit_notes if n not in seen]:
            del self.hit_notes[note]


def lullaby_windows_for(mode: str):
    """The needle sweeps 0..180; each buff scores inside fixed windows.

    The arc position is baked into the texture and cannot be read, so the
    windows are per-buff constants and the user picks the buff. Spellings match
    the settings dropdown, including its "Strenghtening" typo, because the raw
    label text is what gets persisted.
    """
    mode = (mode or "").strip().lower()
    if mode == "quickening":
        return [(0.0, 90.0)]
    if mode in ("strengthening", "strenghtening"):
        return [(76.0, 104.0)]
    if mode == "fortuitous":
        return [(90.0, 180.0)]
    if mode in ("prismatic", "prismatic/serenity", "serenity"):
        return [(0.0, 20.0), (160.0, 180.0)]
    if mode == "resistant":
        return [(0.0, 20.0), (76.0, 104.0), (160.0, 180.0)]
    return []


def lullaby_in_any_window(rotation, windows) -> bool:
    if rotation is None or rotation != rotation:  # NaN never matches
        return False
    return any(lo <= rotation <= hi for lo, hi in windows)


class LullabyController:
    """Lullaby rod: click once per pass through the active buff's window.

    The one-click-per-pass guard matters -- spamming while the needle sits in
    the window kept firing as it swept past the scoring angle, costing progress.
    """

    CLICK_HOLD_MS = 30

    def __init__(self, engine):
        self.engine = engine
        self.clicked_this_pass = False

    def reset(self) -> None:
        self.engine.release_mouse(force=True)
        self.clicked_this_pass = False

    def update(self, ctx=None) -> None:
        game = self.engine.game
        ticker = game.get_metronome_ticker()
        if not ticker:
            self.clicked_this_pass = False
            return

        rotation = game.r.read_frame_rotation(ticker)
        mode = self.engine.userprefs.get("lullaby_mode", "Prismatic")
        if not lullaby_in_any_window(rotation, lullaby_windows_for(mode)):
            self.clicked_this_pass = False  # outside every window -> arm next pass
            return

        if self.clicked_this_pass:
            return

        self.engine.sender.send("{LButton down}")
        time.sleep(self.CLICK_HOLD_MS / 1000.0)
        self.engine.sender.send("{LButton up}")
        self.clicked_this_pass = True
