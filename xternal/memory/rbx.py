"""
XTernal for Mac -- Roblox instance-tree reader.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of shared/Read.ahk and the navigation half of shared/Memory.ahk. The tree
walk itself is unchanged from the original; what differs is the string decoder
(two C++ standard libraries to support) and how the DataModel is located, since
the Android build has no usable static RVA to chase.
"""

from __future__ import annotations

import struct
import time

from .offsets import Offsets, STRING_ABI_LIBCXX, STRING_ABI_MSVC
from .proc import ProcessMemory
from .sso import ALL_LIBCXX, long_fields, sso_decode

#: Instance children are stored as an array of 16-byte entries (a pointer plus
#: refcount bookkeeping); only the leading pointer is of interest.
CHILD_ENTRY_SIZE = 0x10

#: Sanity ceilings lifted straight from the original, to stop a garbage pointer
#: turning into a multi-gigabyte read.
MAX_CHILDREN = 1000
MAX_STRING = 1000

#: How long a parent's child-name table is trusted while its children array is
#: byte-for-byte unchanged. The array check alone catches every add, remove and
#: reorder; the expiry only guards against a freed child's address being reused
#: in place by a differently named instance, which is rare but not impossible.
NAME_TABLE_TTL_S = 1.0

#: Bound on cached parents, so a long session walking many transient GUIs can
#: never grow the cache without limit.
NAME_TABLE_MAX = 512


def is_valid_user_pointer(value: int) -> bool:
    """IsValidUserPointer(): canonical, non-null, below the x86-64 user ceiling."""
    return bool(value) and 0x10000 <= value <= 0x00007FFFFFFFFFFF


class RobloxReader:
    """Typed reads over an attached client, parameterised by an offset table."""

    def __init__(self, mem: ProcessMemory, offsets: Offsets, base: int = 0):
        self.mem = mem
        self.offsets = offsets
        self.base = base
        # Instance-pointer caches, cleared together on re-attach. Same set the
        # original keeps in globals (g_CachedDataModel and friends).
        self._data_model = 0
        self._local_player = 0
        self._player_gui = 0
        self._workspace = 0
        self._world_config = 0
        self._hotbar_gui = 0
        # ClassDescriptor objects are static for the life of the process, so a
        # descriptor's class name never needs reading twice.
        self._class_names: dict[int, str] = {}
        # parent -> (raw children array, [(child, name), ...], fetched_at).
        # Finding a GUI by name used to read every sibling's name on every
        # tick: ~170 reads to locate "reel" among PlayerGui's 85 children.
        self._name_tables: dict[int, tuple[bytes, list, float]] = {}

    # -- cache management -------------------------------------------------

    def clear_caches(self) -> None:
        self._data_model = 0
        self._local_player = 0
        self._player_gui = 0
        self._workspace = 0
        self._world_config = 0
        self._hotbar_gui = 0
        self._name_tables.clear()

    # -- primitives -------------------------------------------------------

    def ptr(self, address: int) -> int:
        return self.mem.u64(address)

    def i32(self, address: int) -> int:
        return self.mem.i32(address)

    def i64(self, address: int) -> int:
        return self.mem.i64(address)

    def u8(self, address: int) -> int:
        return self.mem.u8(address)

    def f32(self, address: int) -> float:
        return self.mem.f32(address)

    # -- strings ----------------------------------------------------------

    def read_string(self, address: int) -> str:
        """Decode a std::string *object* at `address`.

        Both supported clients store instance names and GUI text as a std::string
        by value, but the two standard libraries disagree on the layout, so the
        decoder is chosen by the active offset table's declared ABI.
        """
        if not is_valid_user_pointer(address):
            return ""
        if self.offsets.string_abi in ALL_LIBCXX:
            return self._read_string_libcxx(address)
        return self._read_string_msvc(address)

    def _read_string_msvc(self, address: int) -> str:
        """MSVC layout, as shared/Read.ahk ReadString() did it."""
        length = self.mem.i32(address + self.offsets.get("StringLength", 0x10))
        if length <= 0 or length > MAX_STRING:
            return ""
        data_addr = address
        if length > 15:  # exceeded the SSO buffer -> heap pointer at +0
            data_addr = self.ptr(address)
        if not data_addr:
            return ""
        raw = self.mem.read(data_addr, length)
        if raw is None:
            return ""
        return raw.decode("utf-8", "replace")

    def _read_string_libcxx(self, address: int) -> str:
        """libc++ layout -- standard or alternate, per the offset table (sso.py)."""
        head = self.mem.read(address, 24)
        if head is None:
            return ""
        abi = self.offsets.string_abi
        long = long_fields(head, abi)
        if long is not None:
            length, data_addr = long
            if length <= 0 or length > MAX_STRING:
                return ""
            if not is_valid_user_pointer(data_addr):
                return ""
            raw = self.mem.read(data_addr, length)
            if raw is None:
                return ""
            return raw.decode("utf-8", "replace")
        return sso_decode(head, abi) or ""

    # -- instance basics --------------------------------------------------

    def read_instance_name(self, instance: int) -> str:
        """ReadInstanceName(): Name reaches a std::string through one pointer.

        On the Windows build that pointer lands on the string itself. Newer
        Android builds put it on a small shared block instead, with the string a
        fixed distance in -- NameToString, solved by the calibrator and absent
        (so zero) everywhere the old direct layout still applies.
        """
        if not is_valid_user_pointer(instance):
            return "<null>"
        if self.offsets.get("NameInline"):
            return self.read_string(instance + self.offsets.get("Name")) or "<null>"
        name_ptr = self.ptr(instance + self.offsets.get("Name"))
        if not name_ptr:
            return "<null>"
        return self.read_string(name_ptr + self.offsets.get("NameToString", 0))

    def read_class_name(self, instance: int) -> str:
        """ReadClassName(): Instance -> ClassDescriptor -> ClassName string."""
        if not is_valid_user_pointer(instance):
            return "<unknown>"
        desc = self.ptr(instance + self.offsets.get("ClassDescriptor"))
        if not desc:
            return "<unknown>"
        cached = self._class_names.get(desc)
        if cached is not None:
            return cached
        if self.offsets.get("ClassNameInline"):
            name_ptr = desc + self.offsets.get("ClassDescriptorToClassName")
        else:
            name_ptr = self.ptr(desc + self.offsets.get("ClassDescriptorToClassName"))
        if not name_ptr:
            return "<unknown>"
        name = self.read_string(name_ptr)
        # Only a plausible identifier is worth remembering: a garbage pointer
        # that happened to land on a readable byte must not poison the cache.
        if name and name.isidentifier() and len(self._class_names) < 4096:
            self._class_names[desc] = name
        return name

    def _children_raw(self, instance: int) -> bytes | None:
        """The raw children array; entries are ChildStride bytes, pointer first.

        Children is either a pointer to a {begin, end} vector (Windows,
        Android) or, with ChildrenInline, the {begin, end} pair itself.
        """
        if not is_valid_user_pointer(instance):
            return None
        if self.offsets.get("ChildrenInline"):
            bounds = self.mem.read(instance + self.offsets.get("Children"), 16)
        else:
            list_ptr = self.ptr(instance + self.offsets.get("Children"))
            if not list_ptr:
                return None
            bounds = self.mem.read(list_ptr, 16)
        if bounds is None:
            return None
        start, end = struct.unpack("<QQ", bounds)
        if not start or not end or end <= start:
            return None
        stride = self.offsets.get("ChildStride", CHILD_ENTRY_SIZE)
        count = (end - start) // stride
        if count > MAX_CHILDREN:
            return None
        return self.mem.read(start, count * stride)

    def _unpack_children(self, raw: bytes) -> list[int]:
        # With shared_ptr entries every other u64 is the child pointer; the
        # rest is refcount bookkeeping.
        step = self.offsets.get("ChildStride", CHILD_ENTRY_SIZE) // 8
        return [c for c in memoryview(raw).cast("Q")[::step] if c]

    def read_children(self, instance: int) -> list[int]:
        """ReadChildren(): the children vector is {begin, end} behind one pointer."""
        raw = self._children_raw(instance)
        return self._unpack_children(raw) if raw else []

    def read_parent(self, instance: int) -> int:
        return self.ptr(instance + self.offsets.get("Parent"))

    def named_children(self, instance: int) -> list[tuple[int, str]]:
        """[(child, name), ...], re-reading names only for children that changed.

        The children array is read every call (two syscalls), so additions,
        removals and reordering are always seen immediately; what is skipped is
        re-reading the names of siblings that were already there.
        """
        raw = self._children_raw(instance)
        if not raw:
            self._name_tables.pop(instance, None)
            return []
        now = time.monotonic()
        entry = self._name_tables.get(instance)
        if entry is not None and entry[0] == raw and now - entry[2] < NAME_TABLE_TTL_S:
            return entry[1]
        known = dict(entry[1]) if entry is not None and now - entry[2] < NAME_TABLE_TTL_S else {}
        table = [
            (child, known.get(child) or self.read_instance_name(child))
            for child in self._unpack_children(raw)
        ]
        if len(self._name_tables) >= NAME_TABLE_MAX:
            self._name_tables.clear()
        self._name_tables[instance] = (raw, table, now if not known else entry[2])
        return table

    def find_child_by_name(self, instance: int, name: str) -> int:
        for child, child_name in self.named_children(instance):
            if child_name == name:
                # Confirm the hit with a direct read, so a stale table entry
                # can never hand back the wrong instance.
                if self.read_instance_name(child) == name:
                    return child
                self._name_tables.pop(instance, None)
                break
        else:
            return 0
        for child in self.read_children(instance):
            if self.read_instance_name(child) == name:
                return child
        return 0

    def find_child_by_class(self, instance: int, class_name: str) -> int:
        for child in self.read_children(instance):
            if self.read_class_name(child) == class_name:
                return child
        return 0

    def find_descendant(self, root: int, name: str, class_name: str = "") -> int:
        """Breadth-first descendant search (FindDescendantByNameAndClass)."""
        queue = [root]
        index = 0
        while index < len(queue):
            current = queue[index]
            index += 1
            if self.read_instance_name(current) == name:
                if not class_name or self.read_class_name(current) == class_name:
                    return current
            queue.extend(self.read_children(current))
            if len(queue) > 20000:  # runaway guard the original lacked
                break
        return 0

    def find_descendant_frame(self, root: int, name: str) -> int:
        """FindDescendantFrameByName(): named descendant whose class is Frame."""
        return self.find_descendant(root, name, "Frame")

    def is_cached_addr_valid(self, addr: int, expected_name: str) -> bool:
        """IsCachedAddrValid(): a cached pointer is good while its Name still reads."""
        if not addr:
            return False
        return self.read_instance_name(addr) == expected_name

    # -- properties -------------------------------------------------------

    def read_property_string(self, instance: int, keys: list[str]) -> str:
        """ReadPropertyString(): try each offset as pointer-to-string, then inline."""
        for key in keys:
            if not self.offsets.has(key):
                continue
            offset = self.offsets[key]
            ptr_value = self.ptr(instance + offset)
            if ptr_value:
                text = self.read_string(ptr_value)
                if text:
                    return text
            direct = self.read_string(instance + offset)
            if direct:
                return direct
        return ""

    def read_gui_text(self, instance: int) -> str:
        return self.read_property_string(
            instance, ["Text", "TextLabelText", "ContentText"]
        )

    def read_gui_visible(self, instance: int) -> bool:
        """ReadGuiObjectVisible(): TextLabels and Frames carry the flag separately."""
        if not instance:
            return False
        class_name = self.read_class_name(instance)
        if class_name == "TextLabel" and self.offsets.has("TextLabelVisible"):
            return bool(self.u8(instance + self.offsets["TextLabelVisible"]))
        if self.offsets.has("FrameVisible"):
            return bool(self.u8(instance + self.offsets["FrameVisible"]))
        return True

    # -- geometry ---------------------------------------------------------
    #
    # The original reads UDim2 Position/Size directly and uses the Scale
    # components. That works when the offsets are known exactly. The calibrated
    # Android table may instead resolve only AbsolutePosition/AbsoluteSize
    # (Vector2 floats, far easier to pin down reliably), so both paths exist and
    # produce the same normalised numbers the fishing logic expects.

    def has_udim2(self) -> bool:
        return self.offsets.has("FramePositionX") and self.offsets.has("FrameSizeX")

    def read_frame_position(self, frame: int):
        """UDim2.Position -> (scale_x, offset_x)."""
        base = self.offsets.get("FramePositionX")
        return (self.f32(frame + base), self.i32(frame + base + 0x4))

    def read_frame_size(self, frame: int):
        """UDim2.Size -> (scale_x, offset_x)."""
        base = self.offsets.get("FrameSizeX")
        return (self.f32(frame + base), self.i32(frame + base + 0x4))

    def read_note_position(self, frame: int) -> dict:
        """ReadNotePosition(): full UDim2 as {sx, ox, sy, oy}."""
        base = self.offsets.get("FramePositionX")
        return {
            "sx": self.f32(frame + base),
            "ox": self.i32(frame + base + 0x4),
            "sy": self.f32(frame + base + 0x8),
            "oy": self.i32(frame + base + 0xC),
        }

    def read_frame_rotation(self, frame: int):
        """GuiObject.Rotation in degrees, or None when unavailable."""
        if not frame or not self.offsets.has("FrameRotation"):
            return None
        return self.f32(frame + self.offsets["FrameRotation"])

    def read_absolute_position(self, frame: int):
        return self.mem.f32_pair(frame + self.offsets.get("AbsolutePosition"))

    def read_absolute_size(self, frame: int):
        return self.mem.f32_pair(frame + self.offsets.get("AbsoluteSize"))

    def read_absolute_rect(self, frame: int):
        """(x, y, w, h) in one read when Position and Size sit back to back."""
        pos = self.offsets.get("AbsolutePosition")
        size = self.offsets.get("AbsoluteSize")
        if size == pos + 8:
            raw = self.mem.read(frame + pos, 16)
            return struct.unpack("<ffff", raw) if raw else (0.0, 0.0, 0.0, 0.0)
        return (*self.read_absolute_position(frame), *self.read_absolute_size(frame))

    def frame_scale_x(self, frame: int) -> float:
        """Position.Scale.X, or the absolute-geometry equivalent."""
        if self.has_udim2():
            return self.read_frame_position(frame)[0]
        parent = self.read_parent(frame)
        px, _ = self.read_absolute_position(frame)
        ppx, _, psx, _ = self.read_absolute_rect(parent)
        return (px - ppx) / psx if psx else 0.0

    def frame_scale_width(self, frame: int) -> float:
        """Size.Scale.X, or the absolute-geometry equivalent."""
        if self.has_udim2():
            return self.read_frame_size(frame)[0]
        parent = self.read_parent(frame)
        sx, _ = self.read_absolute_size(frame)
        psx, _ = self.read_absolute_size(parent)
        return sx / psx if psx else 0.0

    def frame_scale_rect(self, frame: int, parent: int = 0):
        """(x, y, w, h) as fractions of the parent, in as few reads as possible.

        The reel controller wants x and width of two frames that share a
        parent every tick; fetching them through frame_scale_x/width costs
        eight reads per frame, this costs two plus one for the parent.
        """
        if self.has_udim2():
            sx, _ = self.read_frame_position(frame)
            sw, _ = self.read_frame_size(frame)
            return sx, self.frame_scale_y(frame), sw, self.frame_scale_height(frame)
        parent = parent or self.read_parent(frame)
        x, y, w, h = self.read_absolute_rect(frame)
        px, py, pw, ph = self.read_absolute_rect(parent)
        if not pw or not ph:
            return 0.0, 0.0, 0.0, 0.0
        return (x - px) / pw, (y - py) / ph, w / pw, h / ph

    def frame_scale_y(self, frame: int) -> float:
        """Position.Scale.Y, or the absolute-geometry equivalent.

        Stays meaningful outside 0..1: rhythm-game notes sit above their lane
        with negative scale, and the controllers key off exactly that.
        """
        if self.has_udim2():
            return self.f32(frame + self.offsets.get("FramePositionX") + 0x8)
        parent = self.read_parent(frame)
        _, py = self.read_absolute_position(frame)
        _, ppy = self.read_absolute_position(parent)
        _, psy = self.read_absolute_size(parent)
        return (py - ppy) / psy if psy else 0.0

    def note_position(self, frame: int) -> dict:
        """{sx, sy} for a note frame, in whichever geometry mode is available."""
        if self.has_udim2():
            pos = self.read_note_position(frame)
            return {"sx": pos["sx"], "sy": pos["sy"]}
        return {"sx": self.frame_scale_x(frame), "sy": self.frame_scale_y(frame)}

    def frame_scale_height(self, frame: int) -> float:
        """Size.Scale.Y, or the absolute-geometry equivalent (power bar fill)."""
        if self.has_udim2():
            return self.f32(frame + self.offsets.get("FrameSizeX") + 0x8)
        parent = self.read_parent(frame)
        _, sy = self.read_absolute_size(frame)
        _, psy = self.read_absolute_size(parent)
        return sy / psy if psy else 0.0

    # -- DataModel resolution ---------------------------------------------

    def resolve_via_fake_data_model(self) -> int:
        if not (
            self.offsets.has("FakeDataModelPointer")
            and self.offsets.has("FakeDataModelToDataModel")
        ):
            return 0
        fake = self.ptr(self.base + self.offsets["FakeDataModelPointer"])
        if not is_valid_user_pointer(fake):
            return 0
        return self.ptr(fake + self.offsets["FakeDataModelToDataModel"])

    def resolve_via_visual_engine(self) -> int:
        for key in (
            "VisualEnginePointer",
            "VisualEngineToDataModel1",
            "VisualEngineToDataModel2",
        ):
            if not self.offsets.has(key):
                return 0
        engine = self.ptr(self.base + self.offsets["VisualEnginePointer"])
        if not is_valid_user_pointer(engine):
            return 0
        fake = self.ptr(engine + self.offsets["VisualEngineToDataModel1"])
        if not is_valid_user_pointer(fake):
            return 0
        return self.ptr(fake + self.offsets["VisualEngineToDataModel2"])

    def set_data_model(self, address: int) -> None:
        """Install a DataModel found by other means (the Android scan path)."""
        self._data_model = address

    def get_data_model(self) -> int:
        """GetDataModel(): cached, with both static chains tried in order.

        On the Android build neither chain is available (no calibrated RVAs), so
        the address is instead installed by the scanner and simply revalidated
        here -- a rejoin swaps the DataModel, and a stale pointer must not stick.
        """
        if self._data_model:
            if self.read_class_name(self._data_model) == "DataModel":
                return self._data_model
            # Rejoined or torn down: drop every pointer derived from the old tree.
            self.clear_caches()

        if not self.offsets.loaded:
            return 0

        dm = self.resolve_via_fake_data_model()
        if not is_valid_user_pointer(dm):
            dm = self.resolve_via_visual_engine()
        if is_valid_user_pointer(dm):
            self._data_model = dm
        return dm

    def try_get_place_id(self) -> int:
        """TryGetPlaceId(): 0 when not resolvable or sitting on the Roblox menu."""
        if not self.offsets.has("PlaceId"):
            return 0
        dm = self.get_data_model()
        if not is_valid_user_pointer(dm):
            return 0
        return self.i64(dm + self.offsets["PlaceId"])

    # -- well-known instances ---------------------------------------------

    def get_players(self) -> int:
        dm = self.get_data_model()
        if not dm:
            return 0
        for child in self.read_children(dm):
            if self.read_class_name(child) == "Players":
                return child
        return 0

    def get_local_player(self) -> int:
        if self._local_player:
            return self._local_player
        players = self.get_players()
        if not players:
            return 0
        lp = self.ptr(players + self.offsets.get("LocalPlayer"))
        if lp:
            self._local_player = lp
        return lp

    def find_player_gui(self) -> int:
        if self._player_gui:
            return self._player_gui
        lp = self.get_local_player()
        if not lp:
            return 0
        for child in self.read_children(lp):
            if self.read_class_name(child) == "PlayerGui":
                self._player_gui = child
                return child
        return 0

    def get_workspace_root(self) -> int:
        if self._workspace:
            return self._workspace
        dm = self.get_data_model()
        if not dm:
            return 0
        for child in self.read_children(dm):
            if (
                self.read_instance_name(child) == "Workspace"
                or self.read_class_name(child) == "Workspace"
            ):
                self._workspace = child
                return child
        return 0

    def get_hotbar_gui(self) -> int:
        """GetHotbarGui(): PlayerGui > backpack > hotbar."""
        if self._hotbar_gui:
            return self._hotbar_gui
        lp = self.get_local_player()
        if not lp:
            return 0
        pg = self.find_child_by_class(lp, "PlayerGui")
        if not pg:
            return 0
        bp = self.find_child_by_name(pg, "backpack")
        if not bp:
            return 0
        hotbar = self.find_child_by_name(bp, "hotbar")
        if hotbar:
            self._hotbar_gui = hotbar
        return hotbar

    def get_world_config(self) -> int:
        """ReplicatedStorage.world -- the authoritative weather/cycle source."""
        if self._world_config:
            return self._world_config
        dm = self.get_data_model()
        if not dm:
            return 0
        rs = self.find_child_by_class(dm, "ReplicatedStorage")
        if not rs:
            return 0
        world = self.find_child_by_name(rs, "world")
        if world:
            self._world_config = world
        return world

    def get_character_model(self) -> int:
        workspace = self.get_workspace_root()
        if not workspace:
            return 0
        lp = self.get_local_player()
        if not lp:
            return 0
        name = self.read_instance_name(lp)
        if not name or name == "<null>":
            return 0
        return self.find_child_by_name(workspace, name)
