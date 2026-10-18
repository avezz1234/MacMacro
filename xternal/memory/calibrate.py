"""
XTernal for Mac -- offset calibration (inherited from the Linux/Android port).

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

There is no Windows counterpart to this file: on Windows the offsets are known
up front, keyed by the client's version-<hash>. The macOS client, like the
Android one the Linux port reads, is a Clang/libc++ build that none of those
numbers apply to, so the layout has to be recovered from the running process.
The pipeline is the Linux one unchanged, plus detection of which libc++ string
layout the client uses (see sso.py).

Nothing here guesses. Every offset is pinned by a property that can be checked
against the live game, and the whole run is rejected unless the final table
passes the same structural test the original used (a DataModel whose children
include Workspace and Players).

Safety
------
Only private anonymous heap regions are ever read (ProcessMemory.scannable_regions).
Device- and DMA-backed mappings are excluded outright; feeding a GPU buffer to
process_vm_readv can take the graphics driver -- and the game -- down with it.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from .offsets import Offsets, STRING_ABI_LIBCXX, STRING_ABI_LIBCXX_ALT
from .proc import ProcessMemory
from .rbx import RobloxReader, is_valid_user_pointer
from .sso import sso_decode, sso_image

CHUNK = 8 << 20

#: Plausible search windows. Instance fields all sit in the first few hundred
#: bytes of the object; GuiObject properties reach further into the subclass.
INSTANCE_FIELD_WINDOW = 0x400

#: How far back from a class-name field the descriptor base may sit. Observed
#: at 0x8 on Windows and Android; widened for macOS, whose layout differs.
DESCRIPTOR_WINDOW = 0x100

#: How far into the block behind Instance.Name the std::string may sit. Observed
#: at 0x0 (older builds, pointer straight at the string) and 0x8 (current
#: Android build, where the pointer lands on a small shared block instead).
NAME_BLOCK_WINDOW = 0x20
GUI_FIELD_WINDOW = 0x1200

#: Instance -> ClassDescriptor pointer offsets to try (0x8..0x80).
CLASS_DESC_OFFSETS = range(0x8, 0x81, 0x8)

#: Children vector entry sizes: shared_ptr (pointer + control block) as on
#: Windows/Android, or a bare pointer.
CHILD_STRIDES = (0x10, 0x8)

#: Give up on the layout search after this long rather than hang the UI.
SOLVE_BUDGET_S = 90.0


@dataclass
class CalibrationResult:
    offsets: Offsets | None = None
    data_model: int = 0
    steps: list[str] = field(default_factory=list)
    #: Raw memory dumps written only when a step fails, for remote diagnosis.
    debug: list[str] = field(default_factory=list)
    ok: bool = False
    error: str = ""

    def log(self, message: str) -> None:
        self.steps.append(message)


class Calibrator:
    def __init__(self, mem: ProcessMemory, progress=None, abi: str = STRING_ABI_LIBCXX):
        self.mem = mem
        self.regions = mem.scannable_regions()
        self._progress = progress
        self.abi = abi
        self._name_cache: dict[tuple, str] = {}
        #: Closest misses of the layout search: (score, description).
        self.near_misses: list[tuple[int, str]] = []

    def _say(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    # -- raw scanning helpers --------------------------------------------

    #: Sweeps are syscall-bound and process_vm_readv releases the GIL, so a
    #: small thread pool is a real speedup. Deliberately modest -- the target
    #: process is a running game and should not be starved of memory bandwidth.
    WORKERS = 4

    def _map_regions(self, fn):
        """Run `fn(region)` across the heap in parallel, returning the results."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            return list(pool.map(fn, self.regions))

    def find_bytes(self, needle: bytes, limit: int = 512) -> list[int]:
        """Every occurrence of `needle` in the heap."""

        def sweep(r):
            hits = []
            for addr, blob in self.mem.read_chunks(r.start, r.end, CHUNK):
                i = blob.find(needle)
                while i != -1:
                    hits.append(addr + i)
                    i = blob.find(needle, i + 1)
            return hits

        out: list[int] = []
        for hits in self._map_regions(sweep):
            out.extend(hits)
            if len(out) >= limit:
                return out[:limit]
        return out

    def find_qwords(self, targets, limit: int = 200000):
        """Addresses whose aligned qword equals one of `targets`.

        Returns [(holder_address, value)]. One pass covers every target.

        Reading the heap is cheap -- ~1s for the whole 4.6 GB -- so the cost is
        entirely in examining it. Unpacking to Python ints and looping costs ~24s
        for the same data, which is why this uses, in order of preference:

        1. numpy, comparing the chunk as a uint64 array (a few hundred ms);
        2. bytes.find per target, which is a C-speed scan and wins outright when
           there are only a handful of targets (the attach path);
        3. the plain Python loop, correct but slow, as a last resort.

        The numpy path deliberately avoids np.isin. Given a uint64 array and a
        list of Python ints, np.isin promotes to float64 and, above the size
        threshold where it switches to its sort-based algorithm, returns wholesale
        garbage: measured here at 13,474,924 false positives out of 13,475,001
        hits on a real heap with 90 targets. searchsorted against an explicit
        uint64 array keeps every comparison exact.
        """
        targets = sorted(set(t for t in targets if t >= 0x10000))
        if not targets:
            return []

        try:
            import numpy as _np
        except ImportError:
            _np = None

        target_set = set(targets)
        target_arr = _np.asarray(targets, dtype=_np.uint64) if _np is not None else None

        def sweep(r):
            hits = []
            for addr, blob in self.mem.read_chunks(r.start, r.end, CHUNK):
                usable = len(blob) & ~7
                if not usable:
                    continue
                if _np is not None:
                    arr = _np.frombuffer(blob, dtype=_np.uint64, count=usable // 8)
                    idx = _np.searchsorted(target_arr, arr)
                    idx[idx == target_arr.size] = 0
                    mask = target_arr[idx] == arr
                    for i in mask.nonzero()[0]:
                        hits.append((addr + int(i) * 8, int(arr[i])))
                elif len(targets) <= 32:
                    for value in targets:
                        needle = struct.pack("<Q", value)
                        i = blob.find(needle)
                        while i != -1:
                            if i % 8 == 0:
                                hits.append((addr + i, value))
                            i = blob.find(needle, i + 1)
                else:
                    values = struct.unpack_from("<%dQ" % (usable // 8), blob, 0)
                    for i, v in enumerate(values):
                        if v in target_set:
                            hits.append((addr + i * 8, v))
            return hits

        out: list[tuple[int, int]] = []
        for hits in self._map_regions(sweep):
            out.extend(hits)
            if len(out) >= limit:
                return out[:limit]
        return out

    def find_qwords_in(self, ranges: list[tuple[int, int]], limit: int = 200000):
        """Same, expressed as 8-aligned ranges (pointers land on 8-byte bounds)."""
        targets = []
        for lo, hi in ranges:
            start = (lo + 7) & ~7
            targets.extend(range(start, hi, 8))
        return self.find_qwords(targets, limit=limit)

    # -- libc++ string helpers -------------------------------------------

    def find_sso(self, text: str, limit: int = 64) -> list[int]:
        """Addresses of short std::string objects holding exactly `text`."""
        needle = sso_image(text, self.abi)
        if self.abi != STRING_ABI_LIBCXX_ALT:
            return self.find_bytes(needle, limit=limit)
        # Alternate layout: the needle is only chars + NUL, so confirm the
        # length byte at +23 (and 8-byte alignment) on every raw hit.
        out = []
        for addr in self.find_bytes(needle, limit=limit * 64):
            if addr % 8 == 0 and self._read_sso(addr) == text:
                out.append(addr)
                if len(out) >= limit:
                    break
        return out

    def _read_sso(self, address: int) -> str:
        """Decode a short libc++ string, or '' if it isn't one."""
        text = sso_decode(self.mem.read(address, 24), self.abi)
        if not text:
            return ""
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            return ""
        return text if text.isprintable() else ""

    # -- the pipeline -----------------------------------------------------

    def run(self, place_id: int = 0) -> CalibrationResult:
        result = CalibrationResult()
        t0 = time.time()

        # Step 1 -- locate the class-name string object for "DataModel".
        # Every Instance reaches its class name through a shared descriptor, so
        # this one string anchors the whole layout.
        total_mb = sum(r.size for r in self.regions) >> 20
        result.log(f"scannable heap: {len(self.regions)} regions, {total_mb} MB")
        self._say("Scanning heap for the DataModel class-name string...")
        strings = []
        for abi in (STRING_ABI_LIBCXX, STRING_ABI_LIBCXX_ALT):
            self.abi = abi
            strings = self.find_sso("DataModel", limit=64)
            result.log(f"string layout {abi}: {len(strings)} 'DataModel' candidates")
            if strings:
                break
        if not strings:
            result.error = (
                "Could not find the DataModel class-name string. "
                "Is the client actually in a game?"
            )
            return result
        result.log(f"string layout: {self.abi}")
        result.log(f"class-name string candidates: {[hex(s) for s in strings]}")

        # Step 2 -- where can the descriptor's name field be? Either a pointer
        # to the string (Windows, Android: "holders" pointing at it), or the
        # string stored inline in the descriptor itself (seen as a possibility
        # on macOS). Both are proposed; the structural test decides.
        self._say("Finding class descriptors...")
        holders = self.find_qwords_in([(s, s + 1) for s in strings], limit=256)
        result.log(f"descriptor name-field candidates: {len(holders)} pointer, "
                   f"{len(strings)} inline")
        targets = [(h, False) for h, _ in holders] + [(s, True) for s in strings]

        # Step 3 -- who points into those descriptors? Those are the Instances.
        self._say("Finding DataModel instances...")
        windows = [(t - DESCRIPTOR_WINDOW, t + 8) for t, _ in targets]
        referrers = self.find_qwords_in(windows, limit=60000)
        result.log(f"instance class-descriptor field candidates: {len(referrers)}")

        # Step 4 -- solve (ClassDescriptor, ClassName, Children) jointly by
        # requiring the structural test the original itself uses.
        self._say("Resolving instance layout...")
        solution = self._solve_instance_layout(targets, referrers, place_id=place_id)
        if not solution:
            result.error = (
                "Found DataModel candidates but none had a readable child list "
                "containing Workspace and Players."
            )
            for score, text in sorted(self.near_misses, reverse=True)[:8]:
                result.log(f"near miss ({score} names decoded): {text}")
            self._dump_failure(strings, holders, referrers, result)
            return result

        table, data_model = solution
        result.data_model = data_model
        result.log(f"DataModel @ {hex(data_model)}")
        for key in ("ClassDescriptor", "ClassDescriptorToClassName", "Children",
                    "ClassNameInline", "ChildrenInline", "ChildStride"):
            if key in table:
                result.log(f"{key} = {hex(table[key])}")

        offsets = Offsets(table, string_abi=self.abi, source="calibrated")
        # Alias, don't copy: later steps write into `table` and the reader they
        # drive has to see each offset the moment it is solved (LocalPlayer in
        # particular, or the PlayerGui lookup that GUI calibration needs fails).
        offsets.table = table
        reader = RobloxReader(self.mem, offsets)
        reader.set_data_model(data_model)

        # Step 5 -- Name, pinned by a child whose class we already know.
        self._say("Resolving Instance.Name...")
        if not self._solve_name(reader, table, data_model):
            result.error = "Could not pin Instance.Name."
            ws = next((c for c in reader.read_children(data_model)
                       if self._class_name_at(c, table) == "Workspace"), 0)
            if ws:
                result.debug.append(f"Workspace instance @ {hex(ws)}:")
                result.debug.extend(self._hexdump(ws, INSTANCE_FIELD_WINDOW))
            return result
        result.log(f"Name = {hex(table['Name'])}" + (" (inline)" if table.get("NameInline") else ""))

        # Step 6 -- Parent, pinned by a child pointing back at the DataModel.
        self._say("Resolving Instance.Parent...")
        if self._solve_parent(reader, table, data_model):
            result.log(f"Parent = {hex(table['Parent'])}")

        # Step 7 -- PlaceId, pinned by the known Fisch place id.
        if place_id:
            self._say("Resolving DataModel.PlaceId...")
            if self._solve_place_id(table, data_model, place_id):
                result.log(f"PlaceId = {hex(table['PlaceId'])}")
            else:
                result.log("PlaceId not found (not in Fisch?) -- left unset")

        # Step 8 -- Players.LocalPlayer.
        self._say("Resolving Players.LocalPlayer...")
        if self._solve_local_player(reader, table):
            result.log(f"LocalPlayer = {hex(table['LocalPlayer'])}")

        # Step 9 -- GUI geometry, pinned by Roblox's own layout equation.
        self._say("Resolving GUI geometry...")
        self._solve_gui_geometry(reader, table, result)

        # Step 10 -- GuiObject.Text, pinned by the hotbar's digit labels.
        self._say("Resolving GuiObject.Text...")
        if self._solve_text(reader, table):
            result.log(f"TextLabelText = {hex(table['TextLabelText'])}")

        # Step 11 -- StringValue.Value, pinned by the world cycle/weather values.
        self._say("Resolving StringValue.Value...")
        if self._solve_value(reader, table):
            result.log(f"Value = {hex(table['Value'])}")

        # Step 12 -- the remaining GuiObject flags, by prediction + verification.
        self._say("Resolving GuiObject flags...")
        self._solve_gui_flags(reader, table, result)

        result.offsets = offsets
        result.ok = True
        result.log(f"calibration completed in {time.time() - t0:.1f}s")
        return result

    # -- step 4 -----------------------------------------------------------

    def _solve_instance_layout(self, targets, referrers, place_id: int = 0):
        """Jointly solve ClassDescriptor / ClassName / Children.

        `targets` are (address, inline) name-field candidates: a pointer to the
        class-name string, or the string itself inside the descriptor. Each
        (target, referrer) pair proposes a ClassName offset and a
        ClassDescriptor offset, and a proposal is only accepted if some field
        of the candidate object decodes as a child list whose members' class
        names include both Workspace and Players -- TestOffsetsInMemory()'s
        criterion.

        Real descriptors are shared by every instance of a class, so busier
        referrer groups are tried first; random heap qwords are singletons.
        """
        groups: dict[int, list[int]] = {}
        for referrer_addr, desc_addr in referrers:
            groups.setdefault(desc_addr, []).append(referrer_addr)
        target_list = sorted(targets)

        def rank(item):
            desc_addr, refs = item
            best = min((t - desc_addr for t, _ in target_list if t >= desc_addr),
                       default=1 << 30)
            return (-len(refs), best)

        deadline = time.monotonic() + SOLVE_BUDGET_S
        fallback = None
        for desc_addr, refs in sorted(groups.items(), key=rank):
            if time.monotonic() > deadline:
                self.near_misses.append((0, "search budget exhausted"))
                break
            for target, inline in target_list:
                if not (desc_addr <= target < desc_addr + DESCRIPTOR_WINDOW + 8):
                    continue
                class_name_off = target - desc_addr
                if class_name_off % 8:
                    continue
                for referrer_addr in refs[:64]:
                    for class_desc_off in CLASS_DESC_OFFSETS:
                        instance = referrer_addr - class_desc_off
                        if not is_valid_user_pointer(instance):
                            continue
                        table = {
                            "ClassDescriptor": class_desc_off,
                            "ClassDescriptorToClassName": class_name_off,
                        }
                        if inline:
                            table["ClassNameInline"] = 1
                        children = self._find_children_offset(instance, table)
                        if children is None:
                            continue
                        table.update(children)

                        # The client runs two DataModels: the Roblox app shell
                        # ("LuaApp", PlaceId 0) and the game itself. Both pass
                        # the structural test; insist on the one in the place
                        # the caller expects, keeping the other as a fallback.
                        if place_id and not self._contains_place_id(instance, place_id):
                            if fallback is None:
                                fallback = (dict(table), instance)
                            continue
                        return table, instance
        return fallback

    def _contains_place_id(self, instance: int, place_id: int) -> bool:
        blob = self.mem.read(instance, GUI_FIELD_WINDOW)
        if not blob:
            return False
        return blob.find(struct.pack("<q", place_id)) != -1

    def _class_name_at(self, instance: int, table: dict) -> str:
        desc = self.mem.u64(instance + table["ClassDescriptor"])
        if not is_valid_user_pointer(desc):
            return ""
        inline = bool(table.get("ClassNameInline"))
        key = (desc, table["ClassDescriptorToClassName"], inline)
        cached = self._name_cache.get(key)
        if cached is not None:
            return cached
        if inline:
            name_ptr = desc + table["ClassDescriptorToClassName"]
        else:
            name_ptr = self.mem.u64(desc + table["ClassDescriptorToClassName"])
            if not is_valid_user_pointer(name_ptr):
                return ""
        name = self._read_sso(name_ptr)
        if len(self._name_cache) < 200000:
            self._name_cache[key] = name
        return name

    def _find_children_offset(self, instance: int, table: dict):
        """Locate the child list whose members include Workspace and Players.

        Tries, at every 8-byte field: a pointer to a {begin, end} vector (the
        Windows/Android layout) and a {begin, end} pair stored inline; each
        with 16-byte (shared_ptr) and 8-byte (raw pointer) entries. Returns the
        offset-table entries describing the match, or None.
        """
        blob = self.mem.read(instance, INSTANCE_FIELD_WINDOW)
        if not blob:
            return None
        for off in range(0, INSTANCE_FIELD_WINDOW - 8, 8):
            first = struct.unpack_from("<Q", blob, off)[0]
            if not is_valid_user_pointer(first):
                continue
            candidates = []
            pair = self.mem.read(first, 16)
            if pair is not None:
                candidates.append((False, *struct.unpack("<QQ", pair)))
            candidates.append((True, first, struct.unpack_from("<Q", blob, off + 8)[0]))
            for inline, start, end in candidates:
                if not is_valid_user_pointer(start) or end <= start or end - start > 0x4000:
                    continue
                entries = self.mem.read(start, end - start)
                if not entries:
                    continue
                for stride in CHILD_STRIDES:
                    span = end - start
                    if span % stride or span // stride < 2:
                        continue
                    found = self._child_classes(entries, stride, table)
                    if found is None:
                        continue
                    if "Workspace" in found and "Players" in found:
                        out = {"Children": off}
                        if inline:
                            out["ChildrenInline"] = 1
                        if stride != 0x10:
                            out["ChildStride"] = stride
                        return out
                    named = sorted(n for n in found if n)
                    if len(named) >= 3:
                        self.near_misses.append((len(named), (
                            f"instance {hex(instance)} ClassDescriptor={hex(table['ClassDescriptor'])} "
                            f"ClassName={hex(table['ClassDescriptorToClassName'])}"
                            f"{' inline' if table.get('ClassNameInline') else ''} "
                            f"children@{hex(off)}{' inline' if inline else ''} stride {stride}: "
                            f"{', '.join(named[:12])}")))
                        if len(self.near_misses) > 200:
                            self.near_misses.sort(reverse=True)
                            del self.near_misses[50:]
        return None

    def _child_classes(self, entries: bytes, stride: int, table: dict):
        """Class names of the entries, or None if the first few don't decode."""
        count = len(entries) // stride
        names = set()
        for i in range(count):
            child = struct.unpack_from("<Q", entries, i * stride)[0]
            if not is_valid_user_pointer(child):
                continue
            names.add(self._class_name_at(child, table))
            # Cheap reject: random pointer pairs almost never decode at all.
            if i == 7 and not any(names):
                return None
        return names if any(names) else None

    # -- diagnostics ------------------------------------------------------

    def _hexdump(self, address: int, size: int) -> list[str]:
        address &= ~0xF
        raw = self.mem.read(address, size)
        if raw is None:
            return [f"  {hex(address)}: <unreadable>"]
        lines = []
        for i in range(0, len(raw), 16):
            chunk = raw[i:i + 16]
            hexpart = " ".join(chunk[j:j + 8].hex() for j in (0, 8))
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            lines.append(f"  {address + i:012x}: {hexpart}  |{text}|")
        return lines

    def _dump_failure(self, strings, holders, referrers, result) -> None:
        """Raw memory around every candidate, so a failure can be diagnosed remotely."""
        d = result.debug
        d.append(f"string abi {self.abi}; DataModel strings:")
        for s in strings[:4]:
            d.append(f"string @ {hex(s)}")
            d.extend(self._hexdump(s - 0x20, 0x60))
        d.append("pointer holders (descriptor name-field candidates):")
        for h, _ in holders[:12]:
            d.append(f"holder @ {hex(h)}")
            d.extend(self._hexdump(h - 0x80, 0xC0))
        groups: dict[int, list[int]] = {}
        for referrer_addr, desc_addr in referrers:
            groups.setdefault(desc_addr, []).append(referrer_addr)
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]
        d.append("busiest referrer groups (descriptor value: count, samples):")
        for desc, refs in top:
            d.append(f"desc {hex(desc)}: {len(refs)} refs, e.g. {[hex(r) for r in refs[:4]]}")
        for desc, refs in top[:6]:
            d.append(f"memory around first referrer of {hex(desc)} ({hex(refs[0])}):")
            d.extend(self._hexdump(refs[0] - 0x80, 0x200))
            d.append(f"descriptor {hex(desc)}:")
            d.extend(self._hexdump(desc, 0x120))

    # -- step 5 -----------------------------------------------------------

    def _solve_name(self, reader: RobloxReader, table: dict, data_model: int) -> bool:
        """Name is the offset that makes the Workspace child report "Workspace".

        Two things stop that from being a one-line search.

        The string is not always at the end of the pointer. Older builds put it
        there; newer ones point at a small shared block and keep the string a
        fixed distance into it, so the inner hop is solved here too and written
        out as NameToString (omitted entirely when it is zero, which is what the
        Windows table and every older calibration expect).

        And "Workspace" is also the *class* name of the object being probed, so
        the class-descriptor chain answers this search correctly and uselessly.
        Skipping that one field is not enough on its own either: a candidate is
        only accepted if it still reads names across a sample of unrelated
        instances, and disagrees with the class name on at least one of them.
        """
        workspace = 0
        for child in reader.read_children(data_model):
            if self._class_name_at(child, table) == "Workspace":
                workspace = child
                break
        if not workspace:
            return False

        sample = self._name_sample(reader, data_model)
        blob = self.mem.read(workspace, INSTANCE_FIELD_WINDOW)
        if not blob:
            return False

        for off in range(0, INSTANCE_FIELD_WINDOW, 8):
            if off == table.get("ClassDescriptor"):
                continue  # reaches the class name, which here reads "Workspace"
            ptr = struct.unpack_from("<Q", blob, off)[0]
            if not is_valid_user_pointer(ptr):
                continue
            for inner in range(0, NAME_BLOCK_WINDOW, 8):
                if self._read_sso(ptr + inner) != "Workspace":
                    continue
                table["Name"] = off
                if inner:
                    table["NameToString"] = inner
                if self._name_offset_holds(reader, table, sample):
                    return True
                table.pop("Name", None)
                table.pop("NameToString", None)

        # macOS may keep the name string inside the instance itself.
        for off in range(0, INSTANCE_FIELD_WINDOW - 24, 8):
            if self._read_sso(workspace + off) != "Workspace":
                continue
            table["Name"] = off
            table["NameInline"] = 1
            if self._name_offset_holds(reader, table, sample):
                return True
            table.pop("Name", None)
            table.pop("NameInline", None)
        return False

    def _name_sample(self, reader: RobloxReader, data_model: int) -> list[int]:
        """Instances to test a candidate Name offset against.

        Services and their children, which between them cover objects whose name
        matches their class ("Players") and objects whose name is arbitrary (a
        Folder in Workspace) -- the second kind is what tells a real Name field
        apart from another route to the class name.
        """
        sample = []
        for child in reader.read_children(data_model)[:24]:
            sample.append(child)
            sample.extend(reader.read_children(child)[:8])
        return sample[:96]

    def _name_offset_holds(self, reader: RobloxReader, table: dict, sample) -> bool:
        """A real Name field reads for most instances and is not the class name."""
        if not sample:
            return True
        decoded = 0
        differs = False
        for instance in sample:
            name = reader.read_instance_name(instance)
            if not name or name == "<null>":
                continue
            decoded += 1
            if name != self._class_name_at(instance, table):
                differs = True
        return differs and decoded >= (len(sample) * 3) // 4

    # -- step 6 -----------------------------------------------------------

    def _solve_parent(self, reader: RobloxReader, table: dict, data_model: int) -> bool:
        children = reader.read_children(data_model)
        if not children:
            return False
        child = children[0]
        blob = self.mem.read(child, INSTANCE_FIELD_WINDOW)
        if not blob:
            return False
        for off in range(0, INSTANCE_FIELD_WINDOW, 8):
            if struct.unpack_from("<Q", blob, off)[0] == data_model:
                table["Parent"] = off
                return True
        return False

    # -- step 7 -----------------------------------------------------------

    def _solve_place_id(self, table: dict, data_model: int, place_id: int) -> bool:
        blob = self.mem.read(data_model, GUI_FIELD_WINDOW)
        if not blob:
            return False
        target = struct.pack("<q", place_id)
        i = blob.find(target)
        if i == -1 or i % 8:
            return False
        table["PlaceId"] = i
        return True

    # -- step 8 -----------------------------------------------------------

    def _solve_local_player(self, reader: RobloxReader, table: dict) -> bool:
        """LocalPlayer is the Players field pointing at the Player owning a PlayerGui.

        Only the local player's PlayerGui is replicated to this client, so that
        child is an unambiguous marker for which Player object is ours.
        """
        players = reader.get_players()
        if not players:
            return False
        local = 0
        for child in reader.read_children(players):
            if self._class_name_at(child, table) != "Player":
                continue
            for grandchild in reader.read_children(child):
                if self._class_name_at(grandchild, table) == "PlayerGui":
                    local = child
                    break
            if local:
                break
        if not local:
            return False
        blob = self.mem.read(players, GUI_FIELD_WINDOW)
        if not blob:
            return False
        for off in range(0, GUI_FIELD_WINDOW - 8, 8):
            if struct.unpack_from("<Q", blob, off)[0] == local:
                table["LocalPlayer"] = off
                return True
        return False

    # -- step 9 -----------------------------------------------------------

    #: Classes that actually carry GuiObject geometry. Layout helpers
    #: (UIListLayout, UIScale, Folder, ...) are children too but have no
    #: rectangle, and including them would poison the containment fit.
    _GUI_OBJECT_CLASSES = (
        "Frame", "ImageLabel", "TextLabel", "ImageButton", "TextButton",
        "ScrollingFrame", "TextBox", "ViewportFrame", "CanvasGroup",
    )

    def _gui_object_pairs(self, reader: RobloxReader, limit: int = 140):
        """(parent, child) pairs of on-screen GuiObjects, for geometry fitting."""
        pg = reader.find_player_gui()
        if not pg:
            return []
        pairs = []
        for sg in reader.read_children(pg):
            if reader.read_class_name(sg) != "ScreenGui":
                continue
            for kid in reader.read_children(sg):
                if reader.read_class_name(kid) not in self._GUI_OBJECT_CLASSES:
                    continue
                for grandkid in reader.read_children(kid):
                    if reader.read_class_name(grandkid) not in self._GUI_OBJECT_CLASSES:
                        continue
                    pairs.append((kid, grandkid))
                    if len(pairs) >= limit:
                        return pairs
        return pairs

    def _gui_samples(self, reader: RobloxReader, table: dict):
        """Collect (parent, child) GuiObject pairs to fit the layout equation."""
        pg = reader.find_player_gui()
        if not pg:
            return []
        pairs = []
        for screen_gui in reader.read_children(pg):
            kids = reader.read_children(screen_gui)
            for kid in kids:
                grandkids = reader.read_children(kid)
                for gk in grandkids:
                    pairs.append((kid, gk))
                    if len(pairs) >= 40:
                        return pairs
        return pairs

    def _solve_gui_geometry(self, reader: RobloxReader, table: dict, result) -> None:
        """Pin AbsolutePosition/AbsoluteSize, then UDim2 Position/Size.

        AbsoluteSize on a ScreenGui equals the viewport, so it is found by
        agreement across several ScreenGuis. The UDim2 fields are then solved by
        requiring Roblox's own layout relation to hold on real parent/child
        pairs:

            child.AbsoluteSize.X     == parent.AbsoluteSize.X * Size.Scale.X + Size.Offset.X
            child.AbsolutePosition.X == parent.AbsolutePosition.X
                                        + parent.AbsoluteSize.X * Position.Scale.X
                                        + Position.Offset.X
        """
        pg = reader.find_player_gui()
        if not pg:
            result.log("no PlayerGui -- GUI geometry skipped")
            return
        screen_guis = [
            sg for sg in reader.read_children(pg)
            if reader.read_class_name(sg) == "ScreenGui"
        ][:40]
        if len(screen_guis) < 4:
            result.log("no ScreenGuis -- GUI geometry skipped")
            return

        # AbsolutePosition and AbsoluteSize are adjacent Vector2s on GuiBase2D,
        # so one base offset B gives both: pos at B, size at B+8.
        #
        # This must be solved on real GuiObjects, not on ScreenGuis. A ScreenGui
        # is a different concrete class and carries other fields that also happen
        # to hold the viewport -- fitting against those yields an offset that
        # reads plausibly on ScreenGuis and returns garbage on every Frame, which
        # silently breaks the cast power bar and the whole reel minigame.
        #
        # The test is geometric: a child GuiObject's rectangle sits inside its
        # parent's. Not universal (icons and strokes do overflow), so the best
        # fit wins rather than requiring unanimity.
        pairs = self._gui_object_pairs(reader)
        if len(pairs) < 8:
            result.log("too few GuiObjects on screen -- GUI geometry skipped")
            return

        blobs: dict[int, bytes] = {}

        def blob_of(addr: int):
            if addr not in blobs:
                blobs[addr] = self.mem.read(addr, GUI_FIELD_WINDOW) or b""
            return blobs[addr]

        limit = 16000.0
        best = None
        for base in range(0, GUI_FIELD_WINDOW - 16, 4):
            fits = 0
            tested = 0
            for parent, child in pairs:
                pb, cb = blob_of(parent), blob_of(child)
                if len(pb) < base + 16 or len(cb) < base + 16:
                    continue
                ppx, ppy, pw, ph = struct.unpack_from("<ffff", pb, base)
                cpx, cpy, cw, ch = struct.unpack_from("<ffff", cb, base)
                tested += 1
                if not (0.5 <= pw <= limit and 0.5 <= ph <= limit):
                    continue
                if not (0.5 <= cw <= limit and 0.5 <= ch <= limit):
                    continue
                if not (-limit <= ppx <= limit and -limit <= ppy <= limit):
                    continue
                if not (-limit <= cpx <= limit and -limit <= cpy <= limit):
                    continue
                tol = 2.0
                if (
                    cpx >= ppx - tol
                    and cpy >= ppy - tol
                    and cpx + cw <= ppx + pw + tol
                    and cpy + ch <= ppy + ph + tol
                ):
                    fits += 1
            if tested >= 8 and fits >= max(8, tested * 0.4):
                candidate = (fits, -base)
                if best is None or candidate > best:
                    best = candidate
                    best_base = base

        if best is None:
            result.log("GuiObject geometry not resolved -- reel tracking unavailable")
            return

        abs_size_off = best_base + 8
        viewport = struct.unpack_from("<ff", blob_of(screen_guis[0]), abs_size_off)
        result.log(
            f"AbsolutePosition = {hex(best_base)}, AbsoluteSize = {hex(abs_size_off)} "
            f"(fit {best[0]}/{len(pairs)}, viewport {viewport[0]:.0f}x{viewport[1]:.0f})"
        )

        table["AbsoluteSize"] = abs_size_off
        table["AbsolutePosition"] = best_base

        pairs = self._gui_samples(reader, table)
        if len(pairs) < 4:
            result.log("too few GUI samples -- UDim2 not resolved (absolute mode)")
            return

        size_off = self._fit_udim2(pairs, table, mode="size")
        pos_off = self._fit_udim2(pairs, table, mode="position")

        # All or nothing. RobloxReader.has_udim2() switches the whole geometry
        # path on when both keys are present, so recording one alone would arm a
        # half-fitted path that reads a wrong field the moment the other turns
        # up. Absolute geometry is already verified working -- prefer it over a
        # partial guess.
        if size_off is not None and pos_off is not None:
            table["FrameSizeX"] = size_off
            table["FramePositionX"] = pos_off
            result.log(
                f"UDim2 Position = {hex(pos_off)}, Size = {hex(size_off)}"
            )
        else:
            # Absolute geometry alone is enough: RobloxReader derives the same
            # normalised fractions from it (see frame_scale_* there).
            result.log("falling back to absolute-geometry mode for frame metrics")

    def _fit_udim2(self, pairs, table: dict, mode: str):
        """Find the offset whose UDim2 reproduces the observed absolute geometry."""
        aps = table["AbsolutePosition"]
        asz = table["AbsoluteSize"]
        best = None
        for off in range(0, GUI_FIELD_WINDOW - 16, 4):
            hits = 0
            tested = 0
            for parent, child in pairs:
                pblob = self.mem.read(parent, GUI_FIELD_WINDOW)
                cblob = self.mem.read(child, GUI_FIELD_WINDOW)
                if not pblob or not cblob:
                    continue
                try:
                    p_size_x = struct.unpack_from("<f", pblob, asz)[0]
                    p_pos_x = struct.unpack_from("<f", pblob, aps)[0]
                    c_size_x = struct.unpack_from("<f", cblob, asz)[0]
                    c_pos_x = struct.unpack_from("<f", cblob, aps)[0]
                    scale, offset = struct.unpack_from("<fi", cblob, off)
                except struct.error:
                    continue
                if p_size_x <= 0 or not (-50.0 < scale < 50.0) or abs(offset) > 20000:
                    continue
                tested += 1
                if mode == "size":
                    predicted = p_size_x * scale + offset
                    actual = c_size_x
                else:
                    predicted = p_pos_x + p_size_x * scale + offset
                    actual = c_pos_x
                if abs(predicted - actual) <= max(1.5, abs(actual) * 0.01):
                    hits += 1
            if tested >= 4 and hits == tested and hits >= 4:
                if best is None or hits > best[1]:
                    best = (off, hits)
        return best[0] if best else None

    # -- step 10 ----------------------------------------------------------

    def _solve_text(self, reader: RobloxReader, table: dict) -> bool:
        """Text is pinned by the hotbar slot labels, which read "1".."9".

        Each ItemTemplate carries a TextLabel named "TextLabel" showing its slot
        digit. The correct offset is the one where those labels decode as short
        strings that are single digits and differ between slots -- a signature no
        constant string in the object can imitate.
        """
        hotbar = reader.get_hotbar_gui()
        if not hotbar:
            return False
        labels = []
        for slot in reader.read_children(hotbar):
            if self._class_name_at(slot, table) != "ImageButton":
                continue
            for child in reader.read_children(slot):
                if (
                    self._class_name_at(child, table) == "TextLabel"
                    and reader.read_instance_name(child) == "TextLabel"
                ):
                    labels.append(child)
        if len(labels) < 2:
            return False

        blobs = [(lbl, self.mem.read(lbl, GUI_FIELD_WINDOW)) for lbl in labels]
        blobs = [(a, b) for a, b in blobs if b]
        for off in range(0, GUI_FIELD_WINDOW - 24, 8):
            digits = []
            for _, blob in blobs:
                text = sso_decode(blob[off : off + 24], self.abi)
                if text is None or len(text) != 1 or not text.isdigit():
                    break
                digits.append(text)
            else:
                if len(set(digits)) == len(digits) and len(digits) >= 2:
                    table["TextLabelText"] = off
                    return True
        return False


    # -- step 11 ----------------------------------------------------------

    def _solve_value(self, reader: RobloxReader, table: dict) -> bool:
        """StringValue.Value, pinned by ReplicatedStorage.world's own children.

        `world` holds StringValues named cycle/weather whose contents are short
        words ("Day", "Night", "None", ...). The right offset is the one that
        decodes to a printable short string on several of them *and* differs
        from the instance's own Name, which rules out accidentally locking onto
        the Name field a second time.
        """
        world = reader.get_world_config()
        if not world:
            return False
        # Only StringValues carry a Value string. `world` also holds Number/Bool
        # values, and a single one of those in the sample fails every offset.
        samples = []
        for child in reader.read_children(world):
            if reader.read_class_name(child) != "StringValue":
                continue
            name = reader.read_instance_name(child)
            if name and name != "<null>":
                samples.append((child, name))
            if len(samples) >= 12:
                break
        if len(samples) < 2:
            return False

        blobs = [(a, n, self.mem.read(a, GUI_FIELD_WINDOW)) for a, n in samples]
        blobs = [(a, n, b) for a, n, b in blobs if b]
        for off in range(0, GUI_FIELD_WINDOW - 24, 8):
            good = 0
            for _, name, blob in blobs:
                text = sso_decode(blob[off : off + 24], self.abi)
                if text is None:
                    break
                if text == "":
                    continue  # an empty value (e.g. clientWeather) proves nothing
                if not text.isascii() or not text.isprintable() or text == name:
                    break
                good += 1
            else:
                if good >= 3:
                    table["Value"] = off
                    return True

        # Nothing matched the strict test -- fall back to the Windows offset and
        # accept it only if it actually decodes. It reads correctly on the
        # Android build (verified live: cycle "Night", weather "Rain"), which is
        # unsurprising since every Instance field also matched exactly.
        for addr, name in samples:
            text = self._read_sso(addr + 0xB8)
            if text and text != name:
                table["Value"] = 0xB8
                return True
        return False

    # -- step 12 ----------------------------------------------------------

    #: GuiObject field offsets in the Windows table. The Android build lays the
    #: same fields out in the same order, so once one anchor is located the rest
    #: follow at a constant delta -- which is then verified, never assumed.
    _WINDOWS_GUI = {
        "AbsoluteSize": 0x100,
        "FramePositionX": 0x510,
        "FrameSizeX": 0x530,
        "ScreenGuiEnabled": 0x4C4,
        "FrameVisible": 0x5AD,
        "TextLabelVisible": 0x5AD,
        "TextLabelText": 0xDA0,
        "FrameRotation": 0x178,
    }

    def _solve_gui_flags(self, reader: RobloxReader, table: dict, result) -> None:
        """Fill the remaining GuiObject fields by predicting, then checking.

        The Instance offsets came back byte-identical to the Windows build, so
        the layout is shared and only subclass fields shift. Using the already
        verified AbsoluteSize as an anchor gives a candidate delta for every
        other GuiObject field; each prediction is then tested against live
        objects and kept only if it holds. Anything that fails verification is
        left unset, and the ported code degrades exactly as the original does
        when an offset is absent (see ReadGuiObjectVisible / IsReelGuiVisible,
        both of which treat a missing offset as "visible").
        """
        if "AbsoluteSize" not in table:
            result.log("no AbsoluteSize anchor -- GuiObject flags left unset")
            return
        delta = table["AbsoluteSize"] - self._WINDOWS_GUI["AbsoluteSize"]
        result.log(f"GuiObject delta vs Windows layout = {delta:#x}")

        pg = reader.find_player_gui()
        screen_guis = reader.read_children(pg)[:10] if pg else []
        frames = []
        for sg in screen_guis:
            for kid in reader.read_children(sg)[:6]:
                frames.append(kid)
            if len(frames) >= 24:
                break

        def predicted(key: str) -> int:
            return self._WINDOWS_GUI[key] + delta

        # ScreenGui.Enabled and GuiObject.Visible are single bytes that must
        # read as a clean 0/1 on every sampled object.
        for key, samples in (
            ("ScreenGuiEnabled", screen_guis),
            ("FrameVisible", frames),
        ):
            off = predicted(key)
            if off < 0 or not samples:
                continue
            values = {self.mem.u8(addr + off) for addr in samples}
            if values and values <= {0, 1}:
                table[key] = off
                if key == "FrameVisible":
                    table["TextLabelVisible"] = off
                result.log(f"{key} = {hex(off)} (verified 0/1 on {len(samples)})")
            else:
                result.log(f"{key} prediction {hex(off)} rejected (saw {sorted(values)[:4]})")

        # Rotation is a float and is almost always exactly 0 on ordinary frames.
        off = predicted("FrameRotation")
        if frames and off >= 0:
            rots = [self.mem.f32(addr + off) for addr in frames]
            if all(-360.0 <= r <= 360.0 and r == r for r in rots):
                table["FrameRotation"] = off
                result.log(f"FrameRotation = {hex(off)} (verified in range)")

        # Text only if the digit-label test did not already pin it exactly.
        if "TextLabelText" not in table:
            off = predicted("TextLabelText")
            if off >= 0:
                table["TextLabelText"] = off
                result.log(f"TextLabelText = {hex(off)} (predicted, unverified)")


def calibrate(mem: ProcessMemory, place_id: int = 0, progress=None) -> CalibrationResult:
    return Calibrator(mem, progress=progress).run(place_id=place_id)


def locate_data_model(
    mem: ProcessMemory, offsets: Offsets, progress=None, place_id: int = 0
) -> int:
    """Find the live DataModel when the offsets are already known.

    This is the attach-time path, and it is the Android stand-in for the static
    FakeDataModelPointer chase the Windows build does in a handful of reads.
    There is no equivalent static pointer available here, so the object has to be
    found by scanning -- but with the offsets in hand each candidate can be
    confirmed outright (class name reads "DataModel", children include Workspace
    and Players).

    Confirming is not enough on its own, though: the client holds **two** live
    DataModels. The Roblox app shell ("LuaApp", PlaceId 0) is a fully populated
    DataModel with both Workspace and Players, sitting alongside the actual game
    ("Ugc", PlaceId = the place you are in). Taking whichever turns up first in
    address order picks the shell about half the time, and everything downstream
    then reports "You're not in Fisch yet". So when a place id is supplied, every
    candidate is collected and the one whose PlaceId matches wins.
    """
    cal = Calibrator(mem, progress=progress, abi=offsets.string_abi)
    reader = RobloxReader(mem, offsets)

    if progress:
        progress("Locating DataModel...")

    strings = cal.find_sso("DataModel", limit=64)
    if not strings:
        return 0

    class_desc_off = offsets.get("ClassDescriptor")
    class_name_off = offsets.get("ClassDescriptorToClassName")
    if offsets.get("ClassNameInline"):
        name_fields = list(strings)  # the string lives inside the descriptor
    else:
        name_fields = [h for h, _ in cal.find_qwords_in([(s, s + 1) for s in strings], limit=256)]
    if not name_fields:
        return 0
    # Descriptor bases implied by the known ClassName offset -- exact values, so
    # the sweep below matches far less noise than a window search would.
    descriptors = {h - class_name_off for h in name_fields}
    descriptors = {d for d in descriptors if is_valid_user_pointer(d)}
    if not descriptors:
        return 0

    def confirm(instance: int) -> bool:
        if reader.read_class_name(instance) != "DataModel":
            return False
        classes = {reader.read_class_name(c) for c in reader.read_children(instance)}
        return "Workspace" in classes and "Players" in classes

    # Only a handful of exact target values here, so this is the fast path.
    candidates = []
    for holder_addr, _ in cal.find_qwords(descriptors, limit=4096):
        instance = holder_addr - class_desc_off
        if not is_valid_user_pointer(instance) or not confirm(instance):
            continue
        if not place_id:
            return instance  # nothing to disambiguate against
        if offsets.has("PlaceId"):
            found_place = reader.i64(instance + offsets["PlaceId"])
            if found_place == place_id:
                return instance
            candidates.append((instance, found_place))
        else:
            candidates.append((instance, 0))

    if candidates and progress:
        progress(
            "No DataModel matched the expected place id; found "
            + ", ".join(f"{hex(a)}(place={p})" for a, p in candidates[:4])
        )
    # Fall back to a real DataModel rather than nothing: the caller reports the
    # place mismatch far more usefully than a bare "could not attach" would.
    return candidates[0][0] if candidates else 0
