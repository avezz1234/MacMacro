"""
XTernal for Mac -- attach management and Fisch game-state accessors.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of the attach half of shared/Memory.ahk, the game-state readers scattered
through shared/Fish.ahk, and all of shared/Totem.ahk's world/hotbar readers.

The attach path differs from Windows in one structural way. There, the DataModel
is reached in a few reads through a static pointer baked into
RobloxPlayerBeta.exe (FakeDataModelPointer). The macOS build has no published
RVA table, so -- exactly as the Linux port does for Android -- the object is
found by a heap scan instead: once per game session, cached, and revalidated
cheaply on every tick.
"""

from __future__ import annotations

import re
import time

from ..log import get_logger
from ..memory.calibrate import is_real_data_model, locate_data_model
from ..memory.offsets import Offsets, load_calibrated_offsets
from ..memory.proc import MemoryAccessError, ProcessMemory, find_client
from ..memory.rbx import RobloxReader, is_valid_user_pointer
from ..settings import FISCH_PLACE_ID, offsets_path

log = get_logger()


def now_ms() -> int:
    """A_TickCount equivalent: a monotonic millisecond counter."""
    return int(time.monotonic() * 1000)


class AttachError(RuntimeError):
    pass


class GameLink:
    """Owns the connection to the running client: process, offsets, reader."""

    def __init__(self):
        self.client = None
        self.mem: ProcessMemory | None = None
        self.offsets: Offsets | None = None
        self.reader: RobloxReader | None = None
        self.attach_fail_reason = ""
        self.status_detail = ""
        #: Whether a client process was seen on the last poll_client(). The
        #: status line used to rescan /proc itself, on every UI refresh: ~10 ms
        #: of syscalls each time, which was most of the macro's idle CPU.
        self.client_present = False

    def poll_client(self):
        """Scan /proc once and remember the outcome; returns the client or None."""
        client = find_client()
        self.client_present = client is not None
        return client

    # -- state ------------------------------------------------------------

    def reset(self) -> None:
        """ResetRobloxAttachmentState(): drop every derived pointer."""
        if self.reader:
            self.reader.clear_caches()
        self.client = None
        self.mem = None
        self.offsets = None
        self.reader = None

    def is_attached(self) -> bool:
        return (
            self.mem is not None
            and self.reader is not None
            and self.mem.alive()
        )

    def is_memory_ready(self) -> bool:
        """IsMemoryReady(): attached, with offsets AND a DataModel that still reads.

        The DataModel check is not redundant. On the Android client the object is
        found by scanning, with no static pointer to re-derive it from, so a
        rejoin leaves the cached address dangling and nothing would ever notice.
        Reporting "not ready" here is what makes the attach watcher run the
        locator again instead of sitting on a dead pointer.
        """
        if not (self.is_attached() and self.offsets and self.offsets.loaded):
            return False
        return is_valid_user_pointer(self.reader.get_data_model())

    def is_in_fisch_game(self) -> bool:
        """The DataModel resolves on the Roblox menu too, so PlaceId is the test."""
        if not self.is_memory_ready():
            return False
        return self.reader.try_get_place_id() == FISCH_PLACE_ID

    # -- attaching --------------------------------------------------------

    def attach(self, progress=None) -> bool:
        """AttachToRoblox(): find the client, load offsets, resolve the DataModel."""
        t0 = time.monotonic()
        client = self.poll_client()
        if client is None:
            self.reset()
            raise AttachError("Roblox is not running.")
        log.info("attach: client %s", client)

        offsets = self._load_offsets_for(client)
        log.info("attach: offsets %s (%d fields, abi=%s)", offsets.source,
                 len(offsets.table), offsets.string_abi)
        try:
            mem = ProcessMemory(client.pid)
        except MemoryAccessError as exc:
            self.attach_fail_reason = "access"
            raise AttachError(str(exc)) from exc
        log.info("attach: memory opened -- %s", mem.region_summary())

        reader = RobloxReader(mem, offsets)
        # Pass the place id so the game DataModel is picked over the Roblox
        # app shell, which is an equally valid-looking DataModel at PlaceId 0.
        data_model = locate_data_model(
            mem, offsets, progress=progress, place_id=FISCH_PLACE_ID
        )
        if data_model:
            reader.set_data_model(data_model)

        if not is_valid_user_pointer(data_model):
            self.attach_fail_reason = "offsets"
            log.warning("attach: DataModel not found after %.1fs", time.monotonic() - t0)
            raise AttachError(
                "Could not find the game in memory. If Roblox just updated, press "
                "Calibrate (from inside a Fisch server)."
            )

        self.client = client
        self.mem = mem
        self.offsets = offsets
        self.reader = reader

        if not self.test_offsets():
            self.attach_fail_reason = "offsets"
            log.warning("attach: DataModel %s failed the structure test", hex(data_model))
            raise AttachError(
                "The offsets no longer match this Roblox build. Press Calibrate."
            )

        self.attach_fail_reason = ""
        place = reader.try_get_place_id()
        log.info("attach: DataModel %s, PlaceId %s, %.1fs", hex(data_model), place,
                 time.monotonic() - t0)
        if not self.is_in_fisch_game():
            raise AttachError("You're not in Fisch yet. Open Fisch, then start the macro.")
        return True

    def _load_offsets_for(self, client) -> Offsets:
        path = offsets_path()
        if not path.exists():
            self.attach_fail_reason = "uncalibrated"
            raise AttachError(
                "Not calibrated yet.\n\n"
                "Join a Fisch server, then press Calibrate (or run "
                "`./xternal-mac calibrate`). It takes under a minute and only has "
                "to be done again when Roblox updates."
            )
        return load_calibrated_offsets(path)

    def test_offsets(self) -> bool:
        """TestOffsetsInMemory(): DataModel whose children include Workspace+Players."""
        r = self.reader
        dm = r.get_data_model()
        if not is_valid_user_pointer(dm):
            return False
        return is_real_data_model(r, r.offsets, dm)

    def status_text(self) -> str:
        """GetAttachStatusText(): what the UI shows about the connection."""
        if not (self.client_present or self.is_attached()):
            return "Waiting for Roblox"
        if not self.is_memory_ready():
            if self.attach_fail_reason == "offsets":
                return "Offsets stale - re-run calibration"
            if self.attach_fail_reason == "uncalibrated":
                return "Setting up - join a Fisch server"
            if self.attach_fail_reason == "access":
                return "Can't read Roblox - run doctor"
            return "Open Fisch to attach"
        if not self.is_in_fisch_game():
            return "Join a Fisch server"
        return "Attached"


# ---------------------------------------------------------------------------
# Fisch-specific readers
# ---------------------------------------------------------------------------

KNOWN_ROD_NAMES = [
    "Bellona's Waraxe", "Pinion's Aria", "Tranquility Rod",
    "Rod Of The Eternal King", "Rod Of The Depths", "Rod Of Time",
    "Flimsy Rod", "Training Rod", "Plastic Rod", "Steady Rod",
    "Reinforced Rod", "Phoenix Rod", "Mythical Rod", "No-Life Rod",
    "Sunken Rod", "Trident Rod", "Kings Rod", "Wisdom Rod",
    "Toxinburst Rod", "The Lost Rod", "Riptide Rod", "Lucid Rod",
    "Celestial Rod", "Seasons Rod", "Krampus's Rod", "Precision Rod",
    "Resourceful Rod", "Toxic Spire Rod", "Gardenkeeper Rod",
    "Voyager Rod", "Vineweaver Rod",
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n+")


def normalize_rod_text(text: str) -> str:
    """NormalizeRodDisplayText(): strip rich-text markup and collapse whitespace."""
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = _TAG_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n", text)
    return text.strip()


def extract_pure_rod_name(text: str) -> str:
    """ExtractPureRodName(): a known rod name, else any line that says "rod"."""
    clean = normalize_rod_text(text)
    if not clean:
        return ""
    for rod in KNOWN_ROD_NAMES:
        if rod in clean:
            return rod
    for line in clean.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line == "Pinion's Aria" or re.search(r"\brod\b", line, re.IGNORECASE):
            return line
    return ""


def rod_names_match(equipped: str, hotbar: str) -> bool:
    """Does the equipped Tool correspond to the hotbar's rod?

    These two strings come from different places and are not expected to be
    identical. The hotbar label carries the rod's mutations ("Empyrean Crested
    Olympian Godbreaker"); the Tool instance on the character is named with the
    base rod alone ("Olympian Godbreaker"). Comparing them for equality marks a
    perfectly good rod as unequipped, so containment either way is the test --
    it needs no list of rods and no list of mutations, both of which go stale
    every time the game adds one.
    """
    a = normalize_rod_text(equipped).casefold()
    b = normalize_rod_text(hotbar).casefold()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _contains(text: str, needle: str) -> bool:
    return needle in normalize_rod_text(text).lower()


def is_pinion_rod(text): return _contains(text, "pinion")
def is_tranquility_rod(text): return _contains(text, "tranquility")
def is_dreambreaker_rod(text): return _contains(text, "dreambreaker")
def is_lullaby_rod(text): return _contains(text, "lullaby")
def is_requiem_rod(text): return _contains(text, "requiem")


def is_bellona_rod(text) -> bool:
    clean = normalize_rod_text(text).lower()
    return "bellona" in clean or "waraxe" in clean


def normalize_hotbar_text(text: str) -> str:
    return _TAG_RE.sub("", text).strip() if text else ""


class FischGame:
    """Every read the fishing logic performs, in one place.

    Thin wrapper over RobloxReader: it adds Fisch's own GUI paths (the reel
    minigame, the hotbar, the rhythm games, the world config) on top of the
    generic instance-tree access.
    """

    def __init__(self, link: GameLink):
        self.link = link
        # Instance-pointer caches for the current catch, cleared per cycle just
        # as the original clears Macro.reelBarAddr and friends.
        self.reel_bar_addr = 0
        self.fish_addr = 0
        self.playerbar_addr = 0
        self.progress_bar_addr = 0
        self.power_bar_addr = 0
        # Per-tick memo. One engine tick asks for the reel GUI and the
        # indicator geometry several times over (visibility, context, progress,
        # then the controller's fish/bar/safe checks); within a tick the answer
        # cannot change, so it is read once.
        self._frame: dict = {}
        self.last_indicator = None
        #: A finished reel still on screen after main.early_cast moved on. It is
        #: skipped until the game removes it, then forgotten -- the next reel
        #: can be allocated at the same address.
        self.stale_reel_gui = 0
        self._rod_state_addr = 0

    def begin_frame(self) -> None:
        """Called at the top of every engine tick: forget last tick's memo."""
        self._frame.clear()

    @property
    def r(self) -> RobloxReader:
        return self.link.reader

    def clear_phase_cache(self) -> None:
        """ClearMacroPhaseCache()."""
        self.reel_bar_addr = 0
        self.fish_addr = 0
        self.playerbar_addr = 0
        self.progress_bar_addr = 0
        self.power_bar_addr = 0
        self.last_indicator = None

    def ready(self) -> bool:
        return self.link.is_memory_ready()

    # -- reel GUI ---------------------------------------------------------

    def get_reel_gui(self) -> int:
        memo = self._frame.get("reel_gui")
        if memo is not None:
            return memo
        pg = self.r.find_player_gui()
        if self.stale_reel_gui:
            gui = 0
            stale_present = False
            for child, name in (self.r.named_children(pg) if pg else []):
                if name != "reel":
                    continue
                if child == self.stale_reel_gui:
                    stale_present = True
                elif not gui:
                    gui = child
            if not stale_present:
                self.stale_reel_gui = 0
        else:
            gui = self.r.find_child_by_name(pg, "reel") if pg else 0
        self._frame["reel_gui"] = gui
        return gui

    def is_reel_gui_visible(self, reel_gui: int = 0) -> bool:
        """IsReelGuiVisible(). Missing offset means "assume visible", as before."""
        reel_gui = reel_gui or self.get_reel_gui()
        if not reel_gui:
            return False
        if not self.r.offsets.has("ScreenGuiEnabled"):
            return True
        return bool(self.r.u8(reel_gui + self.r.offsets["ScreenGuiEnabled"]))

    def get_reel_bar_context(self):
        """GetReelBarContext(): {bar, fish, playerbar}, cached while valid."""
        reel_gui = self.get_reel_gui()
        if not reel_gui:
            self.reel_bar_addr = self.fish_addr = self.playerbar_addr = 0
            self.progress_bar_addr = 0
            return None

        if (
            self.r.is_cached_addr_valid(self.reel_bar_addr, "bar")
            and self.fish_addr
            and self.playerbar_addr
        ):
            return {
                "bar": self.reel_bar_addr,
                "fish": self.fish_addr,
                "playerbar": self.playerbar_addr,
            }

        self.reel_bar_addr = self.fish_addr = self.playerbar_addr = 0
        bar = self.r.find_child_by_name(reel_gui, "bar")
        if not bar:
            return None
        self.reel_bar_addr = bar
        self.fish_addr = self.r.find_child_by_name(bar, "fish")
        self.playerbar_addr = self.r.find_child_by_name(bar, "playerbar")
        return {"bar": bar, "fish": self.fish_addr, "playerbar": self.playerbar_addr}

    def has_active_fishing_context(self, ctx=None) -> bool:
        if ctx is None:
            ctx = self.get_reel_bar_context()
        return bool(ctx and ctx.get("fish") and ctx.get("playerbar"))

    # -- Bellona: every visible reel, not just the first -------------------

    def build_reel_context(self, reel_gui: int):
        if not reel_gui:
            return None
        bar = self.r.find_child_by_name(reel_gui, "bar")
        if not bar:
            return None
        progress_frame = self.r.find_child_by_name(bar, "progress")
        progress_bar = (
            self.r.find_child_by_name(progress_frame, "bar") if progress_frame else 0
        )
        return {
            "reel": reel_gui,
            "bar": bar,
            "fish": self.r.find_child_by_name(bar, "fish"),
            "playerbar": self.r.find_child_by_name(bar, "playerbar"),
            "progress": progress_frame,
            "progressBar": progress_bar,
            "barX": self.r.frame_scale_x(bar),
        }

    def get_reel_contexts(self) -> list:
        """GetReelContexts(), sorted left-to-right by the bar's X position."""
        pg = self.r.find_player_gui()
        if not pg:
            return []
        contexts = []
        for child in self.r.read_children(pg):
            if child == self.stale_reel_gui:
                continue
            if self.r.read_instance_name(child) != "reel":
                continue
            if self.r.read_class_name(child) != "ScreenGui":
                continue
            ctx = self.build_reel_context(child)
            if ctx:
                contexts.append(ctx)
        contexts.sort(key=lambda c: c["barX"])
        return contexts

    # -- progress ---------------------------------------------------------

    def read_progress_bar_percent(self, frame: int) -> float:
        return max(0.0, min(100.0, self.r.frame_scale_width(frame) * 100.0))

    def read_reel_completion_percent(self, ctx):
        if not ctx or not ctx.get("progressBar"):
            return None
        return self.read_progress_bar_percent(ctx["progressBar"])

    def get_reel_progress_context(self):
        reel_gui = self.get_reel_gui()
        if not reel_gui:
            self.progress_bar_addr = 0
            return None
        if self.r.is_cached_addr_valid(self.progress_bar_addr, "bar") and \
           self.r.is_cached_addr_valid(self.reel_bar_addr, "bar"):
            return {"progressBar": self.progress_bar_addr}
        self.progress_bar_addr = 0
        control_bar = (
            self.reel_bar_addr
            if self.r.is_cached_addr_valid(self.reel_bar_addr, "bar")
            else self.r.find_child_by_name(reel_gui, "bar")
        )
        if not control_bar:
            return None
        progress_frame = self.r.find_child_by_name(control_bar, "progress")
        if not progress_frame:
            return None
        progress_bar = self.r.find_child_by_name(progress_frame, "bar")
        if not progress_bar:
            return None
        self.progress_bar_addr = progress_bar
        return {"progressBar": progress_bar}

    def get_fishing_completion_percent(self):
        ctx = self.get_reel_progress_context()
        if not ctx or not ctx.get("progressBar"):
            return None
        return self.read_progress_bar_percent(ctx["progressBar"])

    # -- shake button ---------------------------------------------------

    def get_shake_button(self) -> int:
        """PlayerGui > shakeui > safezone > button, or 0 when not on screen."""
        pg = self.r.find_player_gui()
        ui = self.r.find_child_by_name(pg, "shakeui") if pg else 0
        zone = self.r.find_child_by_name(ui, "safezone") if ui else 0
        return self.r.find_child_by_name(zone, "button") if zone else 0

    def shake_button_signature(self, button: int):
        """Changes every time a shake lands (new button, or the old one moved)."""
        if not button:
            return None
        pos = self.r.read_note_position(button) if self.r.has_udim2() else None
        return (button, tuple(pos.values()) if pos else None)

    # -- rod state ------------------------------------------------------

    def read_rod_state(self):
        """The equipped rod's values/state IntValue, or None if unreadable.

        Observed on Stellarwave Melody: 2 ready to cast, 3 charging, 4 thrown,
        5 lure waiting, 7 reeling, 8 fight won, 9 catch wrapping up.
        """
        addr = self._rod_state_addr
        if not self.r.is_cached_addr_valid(addr, "state"):
            addr = self._rod_state_addr = 0
            character = self.r.get_character_model()
            tool = self.r.find_child_by_class(character, "Tool") if character else 0
            values = self.r.find_child_by_name(tool, "values") if tool else 0
            addr = self.r.find_child_by_name(values, "state") if values else 0
            if not addr:
                return None
            self._rod_state_addr = addr
        raw = self.r.mem.read(addr + self.r.offsets.get("Value", 0xA8), 8)
        if not raw:
            return None
        value = int.from_bytes(raw, "little", signed=True)
        return value if 0 <= value < 64 else None

    # -- cast power bar ---------------------------------------------------

    def resolve_power_bar(self) -> int:
        """ResolvePowerBarPath(): character > HumanoidRootPart > power > bar."""
        if self.r.is_cached_addr_valid(self.power_bar_addr, "bar"):
            return self.power_bar_addr
        self.power_bar_addr = 0
        character = self.r.get_character_model()
        if not character:
            return 0
        root = self.r.find_child_by_name(character, "HumanoidRootPart")
        if not root:
            return 0
        power = self.r.find_child_by_name(root, "power")
        if not power:
            return 0
        bar = self.r.find_descendant_frame(power, "bar")
        if bar:
            self.power_bar_addr = bar
        return bar

    def read_power_bar_percent(self, frame: int) -> float:
        """Cast power is the bar's vertical fill (Size.Scale.Y)."""
        return max(0.0, min(100.0, self.r.frame_scale_height(frame) * 100.0))

    # -- indicator geometry ----------------------------------------------

    def playerbar_center_x(self, playerbar: int) -> float:
        """Centre of the player bar, in the same 0..1 space as the fish.

        The two geometry modes disagree about what "position" means, and getting
        this wrong is not subtle -- it injects a constant error equal to the
        bar's half-width, so the controller corrects in one direction forever and
        pins the bar against an edge.

        UDim2 mode: the player bar has AnchorPoint.X = 0.5, so Position.Scale.X
        already *is* its centre. That is why the Windows original compares
        `fishPos + fishSize/2` against a bare `playerbarPos`.

        Absolute mode: AbsolutePosition is the top-left corner whatever the
        anchor point is, so the half-width has to be added back.
        """
        x = self.r.frame_scale_x(playerbar)
        if self.r.has_udim2():
            return x
        return x + self.r.frame_scale_width(playerbar) / 2.0

    def indicator_positions(self, ctx):
        """(fish_center, playerbar_center, playerbar_half_width) in bar space."""
        if not ctx or not ctx.get("fish") or not ctx.get("playerbar"):
            return None
        key = ("ind", ctx["fish"], ctx["playerbar"])
        if key in self._frame:
            return self._frame[key]
        # fish and playerbar are both children of the reel's "bar" frame, so
        # its rectangle is read once and shared.
        parent = ctx.get("bar") or 0
        fish_x, _, fish_w, _ = self.r.frame_scale_rect(ctx["fish"], parent)
        bar_x, _, bar_w, _ = self.r.frame_scale_rect(ctx["playerbar"], parent)
        # See playerbar_center_x: UDim2 X is already the centre (AnchorPoint
        # 0.5); absolute geometry is the top-left corner.
        bar_center = bar_x if self.r.has_udim2() else bar_x + bar_w / 2.0
        result = (fish_x + fish_w / 2.0, bar_center, bar_w / 2.0)
        self._frame[key] = result
        # Exposed for the UI's live reel replica; costs nothing extra.
        self.last_indicator = result
        return result

    def is_indicator_safe(self, ctx=None):
        """IsIndicatorSafe(): is the fish inside the player bar right now?"""
        if ctx is None:
            ctx = self.get_reel_bar_context()
        parts = self.indicator_positions(ctx)
        if parts is None:
            return None
        fish_center, bar_center, half = parts
        return (bar_center - half) <= fish_center <= (bar_center + half)

    def is_note_in_playerbar(self, x: float, ctx=None, padding: float = 0.0) -> bool:
        if ctx is None:
            ctx = self.get_reel_bar_context()
        if not ctx or not ctx.get("playerbar"):
            return False
        bar_x = self.playerbar_center_x(ctx["playerbar"])
        half = self.r.frame_scale_width(ctx["playerbar"]) / 2.0
        return (bar_x - half - padding) <= x <= (bar_x + half + padding)

    # -- Pinion notes -----------------------------------------------------

    def get_note_container(self) -> int:
        if self.r.is_cached_addr_valid(self.reel_bar_addr, "bar"):
            return self.r.find_child_by_name(self.reel_bar_addr, "noteContainer")
        ctx = self.get_reel_bar_context()
        if not ctx or not ctx.get("bar"):
            return 0
        return self.r.find_child_by_name(ctx["bar"], "noteContainer")

    def get_active_note_target(self):
        """GetActiveNoteTarget(): the lowest on-screen note, as {sx, sy}."""
        container = self.get_note_container()
        if not container:
            return None
        best = None
        best_y = -999999.0
        for name in ("note1", "note2"):
            note = self.r.find_child_by_name(container, name)
            if not note:
                continue
            pos = self.r.note_position(note)
            if pos["sy"] > 0.55 or pos["sy"] < -30:
                continue
            if pos["sy"] > best_y:
                best_y = pos["sy"]
                best = pos
        return best

    # -- Tranquility rhythm game -----------------------------------------

    def get_tranquility_root(self) -> int:
        pg = self.r.find_player_gui()
        if not pg:
            return 0
        gui = self.r.find_child_by_name(pg, "TranquilityRodRhythmGame")
        return self.r.find_child_by_name(gui, "RhythmGame") if gui else 0

    def get_tranquility_lane_container(self, root: int = 0) -> int:
        root = root or self.get_tranquility_root()
        return self.r.find_child_by_name(root, "LaneContainer") if root else 0

    def get_tranquility_lane(self, index: int, container: int = 0) -> int:
        container = container or self.get_tranquility_lane_container()
        return self.r.find_child_by_name(container, f"Lane{index}") if container else 0

    def read_tranquility_progress(self, root: int = 0):
        root = root or self.get_tranquility_root()
        if not root:
            return None
        health = self.r.find_child_by_name(root, "HealthBar")
        fill = self.r.find_child_by_name(health, "Fill") if health else 0
        if not fill:
            return None
        return self.read_progress_bar_percent(fill)

    def get_tranquility_lane_key(self, index: int, root: int = 0, lane: int = 0) -> str:
        fallback = {1: "A", 2: "S", 3: "D", 4: "F"}
        root = root or self.get_tranquility_root()
        label = self.r.find_child_by_name(root, f"KeyLabel{index}") if root else 0
        if not label and lane:
            label = self.r.find_child_by_name(lane, "KeyLabel")
        if label:
            text = self.r.read_gui_text(label).strip()
            if len(text) == 1:
                return text.upper()
        return fallback.get(index, "")

    # -- Lullaby metronome ------------------------------------------------

    def get_metronome_ticker(self) -> int:
        reel_gui = self.get_reel_gui()
        if not reel_gui or not self.is_reel_gui_visible(reel_gui):
            return 0
        bar = self.r.find_child_by_name(reel_gui, "bar")
        details = self.r.find_child_by_name(bar, "Details") if bar else 0
        metronome = self.r.find_child_by_name(details, "Metronome") if details else 0
        return self.r.find_child_by_name(metronome, "Ticker") if metronome else 0

    def is_metronome_active(self) -> bool:
        ticker = self.get_metronome_ticker()
        return bool(ticker and self.r.read_gui_visible(ticker))

    # -- hotbar -----------------------------------------------------------

    def _hotbar_slots(self):
        hotbar = self.r.get_hotbar_gui()
        if not hotbar:
            return
        for slot, name in self.r.named_children(hotbar):
            if name != "ItemTemplate":
                continue
            if self.r.read_class_name(slot) != "ImageButton":
                continue
            yield slot

    def is_hotbar_populated(self) -> bool:
        """True once any slot carries an item name -- the settle clock's trigger."""
        for slot in self._hotbar_slots():
            name_inst = self.r.find_child_by_name(slot, "ItemName")
            if name_inst and self.r.read_gui_text(name_inst).strip():
                return True
        return False

    def get_hotbar_rod_display_text(self) -> str:
        fallback = ""
        for slot in self._hotbar_slots():
            name_inst = self.r.find_child_by_name(slot, "ItemName")
            if not name_inst:
                continue
            text = normalize_rod_text(self.r.read_gui_text(name_inst))
            if not text:
                continue
            if (
                extract_pure_rod_name(text)
                or is_bellona_rod(text)
                or is_pinion_rod(text)
                or is_tranquility_rod(text)
                or is_lullaby_rod(text)
                or is_requiem_rod(text)
            ):
                return text
            if not fallback:
                fallback = text
        return fallback

    def get_hotbar_rod_name(self) -> str:
        fallback = ""
        for slot in self._hotbar_slots():
            name_inst = self.r.find_child_by_name(slot, "ItemName")
            if not name_inst:
                continue
            raw = self.r.read_gui_text(name_inst)
            pure = extract_pure_rod_name(raw)
            if pure:
                return pure
            text = normalize_rod_text(raw)
            if text and not fallback:
                fallback = text
        return fallback

    def read_hotbar_item_name(self, slot: int) -> str:
        name_inst = self.r.find_child_by_name(slot, "ItemName")
        if not name_inst:
            return ""
        return normalize_hotbar_text(self.r.read_gui_text(name_inst))

    def read_hotbar_item_slot_key(self, slot: int) -> str:
        for child in self.r.read_children(slot):
            if (
                self.r.read_class_name(child) == "TextLabel"
                and self.r.read_instance_name(child) == "TextLabel"
            ):
                return normalize_hotbar_text(self.r.read_gui_text(child))
        return ""

    def find_hotbar_item_by_name(self, item_name: str) -> int:
        for slot in self._hotbar_slots():
            if self.read_hotbar_item_name(slot) == item_name:
                return slot
        return 0

    def get_hotbar_item_slot_key(self, item_name: str) -> str:
        slot = self.find_hotbar_item_by_name(item_name)
        return self.read_hotbar_item_slot_key(slot) if slot else ""

    # -- equipped tool ----------------------------------------------------

    def get_equipped_tool_name(self) -> str:
        character = self.r.get_character_model()
        if not character:
            return ""
        for child in self.r.read_children(character):
            if self.r.read_class_name(child) == "Tool":
                return self.r.read_instance_name(child)
        return ""

    def is_anything_equipped(self) -> bool:
        character = self.r.get_character_model()
        if not character:
            return False
        return any(
            self.r.read_class_name(c) == "Tool" for c in self.r.read_children(character)
        )

    def is_rod_equipped(self) -> bool:
        equipped = self.get_equipped_tool_name()
        if not equipped:
            return False
        rod = self.get_hotbar_rod_name()
        if rod and rod_names_match(equipped, rod):
            return True
        # Falls through when the hotbar text could not be read *or* did not
        # line up: a tool that calls itself a rod still counts, which keeps a
        # mismatch from turning into an unequip loop.
        return "rod" in equipped.casefold()

    # -- world state (weather / day cycle) --------------------------------

    def read_world_string_value(self, instance: int) -> str:
        """StringValue.Value: inline std::string first, pointer as a fallback."""
        if not instance:
            return ""
        offset = self.r.offsets.get("Value", 0xB8)
        embedded = self.r.read_string(instance + offset)
        if embedded:
            return embedded
        ptr = self.r.ptr(instance + offset)
        return self.r.read_string(ptr) if ptr else ""

    def _world_child(self, name: str) -> int:
        world = self.r.get_world_config()
        return self.r.find_child_by_name(world, name) if world else 0

    def get_current_cycle(self) -> str:
        return self.read_world_string_value(self._world_child("cycle")).strip()

    def get_current_weather(self) -> str:
        return self.read_world_string_value(self._world_child("weather")).strip()

    def get_current_meteorological(self) -> str:
        weather = self._world_child("weather")
        if not weather:
            return ""
        child = self.r.find_child_by_name(weather, "meteorological")
        value = self.read_world_string_value(child).strip()
        return "" if value.lower() == "none" else value

    def is_night_cycle(self) -> bool:
        return "night" in self.get_current_cycle().lower()

    def is_aurora_active(self) -> bool:
        if "aurora" in self.get_current_meteorological().lower():
            return True
        return "aurora" in self.get_current_weather().lower()

    def is_totem_blocked(self) -> bool:
        """Starfall and Rainbow must not be overwritten by a totem pop."""
        met = self.get_current_meteorological().lower()
        weather = self.get_current_weather().lower()
        return any(w in met or w in weather for w in ("starfall", "rainbow"))

    # -- appraise ---------------------------------------------------------

    def resolve_fish_info_subvalues(self) -> int:
        character = self.r.get_character_model()
        if not character:
            return 0
        fish_info = self.r.find_child_by_name(character, "fishinfo")
        info = self.r.find_child_by_name(fish_info, "Info") if fish_info else 0
        return self.r.find_child_by_name(info, "Subvalues") if info else 0

    def collect_subvalues_text(self, subvalues: int) -> str:
        parts = []

        def append(instance: int) -> None:
            class_name = self.r.read_class_name(instance)
            if "Text" not in class_name and "Value" not in class_name:
                return
            text = self.r.read_gui_text(instance)
            if not text and "Value" in class_name:
                text = self.r.read_property_string(instance, ["Value"])
            text = text.strip()
            if text:
                parts.append(text)

        append(subvalues)
        for child in self.r.read_children(subvalues):
            append(child)
            for grandchild in self.r.read_children(child):
                append(grandchild)
        return " ".join(parts)

    def read_current_coins(self):
        pg = self.r.find_player_gui()
        hud = self.r.find_child_by_name(pg, "hud") if pg else 0
        safezone = self.r.find_child_by_name(hud, "safezone") if hud else 0
        coins = self.r.find_child_by_name(safezone, "coins") if safezone else 0
        if not coins:
            return None
        digits = re.sub(r"\D", "", self.r.read_gui_text(coins))
        return int(digits) if digits else None
