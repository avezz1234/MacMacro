"""
XTernal for Mac -- process discovery and raw memory access.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Replaces the Windows OpenProcess/ReadProcessMemory/EnumProcessModulesEx layer
(shared/Process.ahk, shared/Read.ahk) -- and the Linux port's process_vm_readv
layer -- with the Mach equivalents:

    OpenProcess + ReadProcessMemory  ->  task_for_pid + mach_vm_read_overwrite
    VirtualQueryEx / /proc/maps      ->  mach_vm_region_recurse
    ProcessExist("RobloxPlayerBeta") ->  pgrep RobloxPlayer

The class keeps the Linux ProcessMemory interface exactly (read, u8..f64,
read_chunks, scannable_regions), so the calibrator and the instance reader run
on top of it unchanged.

Why this needs root and relaxed SIP
-----------------------------------
task_for_pid is the only way to read another process's memory on macOS. It
needs root, and Roblox ships with the Hardened Runtime, which -- while System
Integrity Protection's debugging restrictions are on -- refuses task_for_pid
even to root. `xternal-mac doctor` detects both and says what to do.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import threading
from dataclasses import dataclass

from ..macos import native

#: Reads at or below this size go through a reused per-thread buffer.
_SMALL_READ = 4096

#: Above this, a region has to be mostly resident to be worth sweeping --
#: allocators reserve huge spans they barely touch (see the Linux notes).
LARGE_REGION = 64 << 20

_U32 = struct.Struct("<I").unpack_from
_I32 = struct.Struct("<i").unpack_from
_U64 = struct.Struct("<Q").unpack_from
_I64 = struct.Struct("<q").unpack_from
_F32 = struct.Struct("<f").unpack_from
_F64 = struct.Struct("<d").unpack_from

try:
    _PAGE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    _PAGE = 16384

#: VM user tags whose memory belongs to the GPU / window server, never the
#: engine heap. Excluded as a backstop on top of the shared-memory filter.
_DENY_TAGS = {21, 62, 63, 64, 65, 66, 67}


class MemoryAccessError(RuntimeError):
    """task_for_pid failed. The message says why, in plain words."""


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    protection: int
    user_tag: int
    share_mode: int
    resident: int

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def perms(self) -> str:
        p = self.protection
        return (
            ("r" if p & native.VM_PROT_READ else "-")
            + ("w" if p & native.VM_PROT_WRITE else "-")
            + ("x" if p & native.VM_PROT_EXECUTE else "-")
        )


def sip_status_text() -> str:
    """Raw `csrutil status` output, or '' if it could not be run."""
    try:
        return subprocess.run(
            ["/usr/bin/csrutil", "status"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def sip_allows_debugging(text: str | None = None) -> bool | None:
    """True if SIP is off or its debugging restriction is off; None if unknown."""
    text = sip_status_text() if text is None else text
    if not text:
        return None
    low = text.lower()
    if "debugging restrictions: disabled" in low:
        return True
    if "status: disabled" in low:
        return True
    if "status: enabled" in low:
        return False
    return None


def explain_task_failure(kr: int) -> str:
    """Turn a task_for_pid failure into something a person can act on."""
    lines = [f"Could not open Roblox's memory (task_for_pid: {native.mach_error(kr)}, code {kr})."]
    if os.geteuid() != 0:
        lines.append(
            "The macro is not running as root. Start it with XTernal.command "
            "(it asks for your password)."
        )
        return "\n".join(lines)
    sip = sip_allows_debugging()
    if sip is False:
        lines.append(
            "System Integrity Protection is blocking it. Roblox uses Apple's "
            "Hardened Runtime, and while SIP's debugging restrictions are on, macOS "
            "refuses to let anything (even root) read its memory. See the "
            "'Turn off SIP debugging restrictions' section of the README."
        )
    else:
        lines.append(
            "Running as root and SIP looks relaxed, so this is unexpected. Restart "
            "Roblox and try again; if it persists, run `./xternal-mac report` and "
            "send the zip."
        )
    return "\n".join(lines)


# One send right per pid for the life of the process: task_for_pid hands out a
# fresh right on every call, and the attach watcher would otherwise leak them.
_tasks: dict[int, int] = {}
_tasks_lock = threading.Lock()


def open_task(pid: int) -> int:
    with _tasks_lock:
        port = _tasks.get(pid)
        if port:
            return port
        kr, port = native.task_for_pid(pid)
        if kr != native.KERN_SUCCESS or not port:
            raise MemoryAccessError(explain_task_failure(kr))
        # Drop rights for pids that are gone.
        for old_pid in list(_tasks):
            if not _pid_alive(old_pid):
                native.deallocate_port(_tasks.pop(old_pid))
        _tasks[pid] = port
        return port


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class _Scratch(threading.local):
    """One buffer per thread: ctypes releases the GIL mid-call."""

    def __init__(self):
        self.buf = ctypes.create_string_buffer(_SMALL_READ)
        self.addr = ctypes.addressof(self.buf)
        self.out = ctypes.c_uint64(0)


class ProcessMemory:
    """Read-only view of another process's address space."""

    def __init__(self, pid: int):
        self.pid = pid
        self.task = open_task(pid)
        self._scratch = _Scratch()
        self._regions_cache: list[Region] | None = None

    # -- liveness ---------------------------------------------------------

    def alive(self) -> bool:
        return _pid_alive(self.pid)

    # -- raw reads --------------------------------------------------------

    def read(self, address: int, size: int) -> bytes | None:
        """Return `size` bytes at `address`, or None if unreadable."""
        if address <= 0 or size <= 0 or address > 0xFFFFFFFFFFFFFFFF - size:
            return None
        if size <= _SMALL_READ:
            s = self._scratch
            s.out.value = 0
            kr = native.vm_read_into(self.task, address, size, s.addr, s.out)
            if kr != 0 or s.out.value != size:
                return None
            return s.buf.raw[:size]
        buf = ctypes.create_string_buffer(size)
        out = ctypes.c_uint64(0)
        kr = native.vm_read_into(self.task, address, size, ctypes.addressof(buf), out)
        if kr != 0 or out.value != size:
            return None
        return buf.raw

    def u8(self, address: int) -> int:
        b = self.read(address, 1)
        return b[0] if b else 0

    def u32(self, address: int) -> int:
        b = self.read(address, 4)
        return _U32(b)[0] if b else 0

    def i32(self, address: int) -> int:
        b = self.read(address, 4)
        return _I32(b)[0] if b else 0

    def u64(self, address: int) -> int:
        b = self.read(address, 8)
        return _U64(b)[0] if b else 0

    def i64(self, address: int) -> int:
        b = self.read(address, 8)
        return _I64(b)[0] if b else 0

    def f32(self, address: int) -> float:
        b = self.read(address, 4)
        return _F32(b)[0] if b else 0.0

    def f64(self, address: int) -> float:
        b = self.read(address, 8)
        return _F64(b)[0] if b else 0.0

    def f32_pair(self, address: int) -> tuple[float, float]:
        b = self.read(address, 8)
        return struct.unpack("<ff", b) if b else (0.0, 0.0)

    def ptr(self, address: int) -> int:
        return self.u64(address)

    # -- regions ----------------------------------------------------------

    def regions(self, refresh: bool = False) -> list[Region]:
        if self._regions_cache is not None and not refresh:
            return self._regions_cache
        out = []
        for start, size, info in native.vm_regions(self.task):
            out.append(Region(
                start=start,
                end=start + size,
                protection=info.protection,
                user_tag=info.user_tag,
                share_mode=info.share_mode,
                resident=info.pages_resident * _PAGE,
            ))
        self._regions_cache = out
        return out

    def read_chunks(self, start: int, end: int, chunk: int = 4 << 20):
        """Yield (address, bytes) over [start, end), tolerating holes.

        A Mach read is all-or-nothing, so a chunk straddling an unreadable page
        fails whole. On failure the range is split and retried down to page
        granularity, so only the genuinely absent pages are lost.
        """
        addr = start
        while addr < end:
            size = min(chunk, end - addr)
            blob = self.read(addr, size)
            if blob is not None:
                yield addr, blob
                addr += size
                continue
            if size <= _PAGE:
                addr += size
                continue
            half = max(_PAGE, (size // 2) & ~(_PAGE - 1))
            yield from self.read_chunks(addr, addr + half, half)
            addr += half

    def scannable_regions(self, max_region: int = 4 << 30) -> list[Region]:
        """Resident, private, read-write anonymous memory -- the engine heap.

        Same idea as the Linux filter: only memory the allocator handed out,
        never shared/GPU/window-server mappings, and never a huge reservation
        the allocator barely touched.
        """
        out = []
        for r in self.regions(refresh=True):
            if (r.protection & (native.VM_PROT_READ | native.VM_PROT_WRITE)) != (
                native.VM_PROT_READ | native.VM_PROT_WRITE
            ):
                continue
            if r.share_mode not in (native.SM_PRIVATE, native.SM_COW,
                                    native.SM_PRIVATE_ALIASED, native.SM_LARGE_PAGE):
                continue
            if r.user_tag in _DENY_TAGS:
                continue
            if r.size < 0x1000 or r.size > max_region:
                continue
            if r.resident < 0x10000:
                continue
            if r.size > LARGE_REGION and r.resident < r.size // 8:
                continue
            out.append(r)
        return out

    def region_summary(self) -> str:
        """One line for the log: how much was scannable, by VM tag."""
        regs = self.scannable_regions()
        by_tag: dict[int, int] = {}
        for r in regs:
            by_tag[r.user_tag] = by_tag.get(r.user_tag, 0) + r.size
        tags = ", ".join(
            f"tag{t}={s >> 20}MB" for t, s in sorted(by_tag.items(), key=lambda kv: -kv[1])[:8]
        )
        total = sum(r.size for r in regs) >> 20
        return f"{len(regs)} regions, {total} MB scannable ({tags})"


# ---------------------------------------------------------------------------
# Client discovery
# ---------------------------------------------------------------------------

CLIENT_MAC = "mac"

#: Executable names of the Roblox player on macOS. Studio is deliberately absent.
PLAYER_NAMES = ("RobloxPlayer",)


@dataclass(frozen=True)
class ClientProcess:
    pid: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind} pid={self.pid} ({self.detail})"


def _pgrep(*args: str) -> list[int]:
    try:
        out = subprocess.run(
            ["/usr/bin/pgrep", *args], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def find_client() -> ClientProcess | None:
    """The newest running Roblox player process, or None."""
    pids: list[int] = []
    for name in PLAYER_NAMES:
        pids += _pgrep("-x", name)
    if not pids:
        pids = _pgrep("-f", "Roblox.app/Contents/MacOS/RobloxPlayer")
    if not pids:
        return None
    pid = max(pids)  # newest; pids grow monotonically between reboots in practice
    path = ""
    try:
        path = native.proc_path(pid)
    except Exception:
        pass
    return ClientProcess(pid, CLIENT_MAC, path or "RobloxPlayer")


def find_client_pid() -> int:
    c = find_client()
    return c.pid if c else 0
