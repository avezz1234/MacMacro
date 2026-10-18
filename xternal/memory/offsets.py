"""
XTernal for Mac -- offset tables.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of the flattening half of shared/Memory.ahk (LoadOffsets /
ApplyParsedOffsets / OffsetRenameMap), plus the piece the Windows original never
needed: a second, structurally different offset set for the Android build.

Why two sets exist
------------------
The bundled settings/offsets.json describes RobloxPlayerBeta.exe: an MSVC-built
PE. Under Sober the client is Roblox's *Android x86_64* build, compiled with
Clang against libc++ and shipped inside com.roblox.client/base.apk. Same engine
source, different compiler and different C++ standard library, so essentially
none of the numbers transfer:

  * every static RVA (FakeDataModelPointer, VisualEnginePointer) is meaningless;
  * std::string is laid out differently (see STRING_ABI_* below);
  * struct field offsets shift with vtable/padding differences.

So the Windows table is kept verbatim for the Wine host, and the Sober host gets
its numbers from a calibration pass (see calibrate.py) cached on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: MSVC std::string: capacity at +0x10 is the length field the original reads,
#: data is inline for length <= 15 and behind a pointer at +0x00 otherwise.
STRING_ABI_MSVC = "msvc"

#: libc++ std::string (Android/Linux). Short form: byte 0 holds (len << 1) and
#: the characters start at +1, holding up to 22 of them. Long form: byte 0's low
#: bit is set, capacity at +0x00, length at +0x08, data pointer at +0x10.
STRING_ABI_LIBCXX = "libcxx"

#: libc++'s alternate string layout (chars first, length in byte 23). Some
#: Apple toolchains build with it; see sso.py. The calibrator detects which one
#: the live client uses.
STRING_ABI_LIBCXX_ALT = "libcxx_alt"

_HERE = Path(__file__).resolve().parent

#: Windows offsets.json -> flat key names, exactly as OffsetRenameMap() does it.
#: (category, field, flat_name). Order matters: later entries overwrite earlier.
_RENAME_MAP = [
    ("FakeDataModel", "Pointer", "FakeDataModelPointer"),
    ("FakeDataModel", "RealDataModel", "FakeDataModelToDataModel"),
    ("VisualEngine", "Pointer", "VisualEnginePointer"),
    ("VisualEngine", "FakeDataModel", "VisualEngineToDataModel1"),
    ("FakeDataModel", "RealDataModel", "VisualEngineToDataModel2"),
    ("DataModel", "PlaceId", "PlaceId"),
    ("Player", "LocalPlayer", "LocalPlayer"),
    ("Instance", "Name", "Name"),
    ("Instance", "ClassDescriptor", "ClassDescriptor"),
    ("Instance", "ClassName", "ClassDescriptorToClassName"),
    ("Instance", "ChildrenStart", "Children"),
    ("Instance", "Parent", "Parent"),
    ("Misc", "StringLength", "StringLength"),
    ("Misc", "Value", "Value"),
    ("GuiObject", "Text", "TextLabelText"),
    ("GuiObject", "Visible", "TextLabelVisible"),
    ("GuiObject", "Visible", "FrameVisible"),
    ("GuiObject", "ScreenGui_Enabled", "ScreenGuiEnabled"),
    ("GuiObject", "Position", "FramePositionX"),
    ("GuiObject", "Size", "FrameSizeX"),
    ("GuiObject", "Rotation", "FrameRotation"),
]


def _to_int(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text, 0)


class Offsets:
    """A flat offset table plus the ABI details that go with it."""

    def __init__(
        self,
        table: dict[str, int] | None = None,
        *,
        string_abi: str = STRING_ABI_MSVC,
        build_version: str = "",
        source: str = "",
    ):
        self.table: dict[str, int] = dict(table or {})
        self.string_abi = string_abi
        self.build_version = build_version
        self.source = source

    # dict-ish access so ported code reads like the AHK original
    def __contains__(self, key: str) -> bool:
        return key in self.table

    def __getitem__(self, key: str) -> int:
        return self.table[key]

    def get(self, key: str, default: int = 0) -> int:
        return self.table.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.table

    def __bool__(self) -> bool:
        return bool(self.table)

    @property
    def loaded(self) -> bool:
        """Mirrors AreOffsetsLoaded(): a table without the tree basics is useless."""
        return bool(self.table) and "ClassDescriptor" in self.table

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.build_version,
                "source": self.source,
                "string_abi": self.string_abi,
                "offsets": {k: hex(v) for k, v in sorted(self.table.items())},
            },
            indent=2,
        )


def load_windows_offsets(path: str | os.PathLike | None = None) -> Offsets:
    """Flatten the bundled Windows offsets.json (v1 Title-Case or v2 lowercase)."""
    p = Path(path) if path else _HERE / "offsets.windows.json"
    with open(p, "r", encoding="utf-8") as fh:
        parsed = json.load(fh)

    version = parsed.get("version") or parsed.get("Roblox Version") or ""
    nested = parsed.get("offsets") or parsed.get("Offsets")
    if nested is None:
        raise ValueError("'offsets' section not found in offsets.json")

    flat: dict[str, int] = {}
    for category, field, flat_name in _RENAME_MAP:
        cat = nested.get(category)
        if not isinstance(cat, dict) or field not in cat:
            continue
        flat[flat_name] = _to_int(cat[field])

    if "FakeDataModelPointer" not in flat:
        raise ValueError("FakeDataModelPointer not found in offsets")

    return Offsets(
        flat,
        string_abi=STRING_ABI_MSVC,
        build_version=version,
        source=str(p),
    )


def load_calibrated_offsets(path: str | os.PathLike) -> Offsets:
    """Load an offsets file produced by the calibrator (already flat)."""
    with open(path, "r", encoding="utf-8") as fh:
        parsed = json.load(fh)
    table = {k: _to_int(v) for k, v in (parsed.get("offsets") or {}).items()}
    return Offsets(
        table,
        string_abi=parsed.get("string_abi", STRING_ABI_LIBCXX),
        build_version=parsed.get("version", ""),
        source=str(path),
    )


def save_calibrated_offsets(offsets: Offsets, path: str | os.PathLike) -> None:
    from ..settings import give_back

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(offsets.to_json(), encoding="utf-8")
    give_back(path)
