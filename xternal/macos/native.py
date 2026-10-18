"""
XTernal for Mac -- ctypes bindings to the macOS system frameworks.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Everything platform-specific the port needs, in one place, with no compiled
dependencies (no pyobjc, no extension modules) so installing is just
"pip install PySide6 numpy":

    Mach VM          task_for_pid, mach_vm_read_overwrite, mach_vm_region_recurse
    CoreFoundation   just enough to walk the window list's CFArray/CFDictionary
    CoreGraphics     CGEvent (synthetic keys and clicks), CGWindowList, displays
    HIServices       AXIsProcessTrusted (the Accessibility permission)

Importing this module on a non-Mac does not fail; `AVAILABLE` is False and every
call raises NativeUnavailable. That keeps the rest of the package importable
(and testable) anywhere.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_int32,
    c_int64,
    c_long,
    c_ubyte,
    c_uint,
    c_uint16,
    c_uint32,
    c_uint64,
    c_ushort,
    c_void_p,
)

IS_MAC = sys.platform == "darwin"


class NativeUnavailable(RuntimeError):
    pass


class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class CGRect(Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class VMRegionSubmapInfo64(Structure):
    """vm_region_submap_info_64 (mach/vm_region.h), packed to 4 like the header."""

    _pack_ = 4
    _fields_ = [
        ("protection", c_int),
        ("max_protection", c_int),
        ("inheritance", c_uint),
        ("offset", c_uint64),
        ("user_tag", c_uint),
        ("pages_resident", c_uint),
        ("pages_shared_now_private", c_uint),
        ("pages_swapped_out", c_uint),
        ("pages_dirtied", c_uint),
        ("ref_count", c_uint),
        ("shadow_depth", c_ushort),
        ("external_pager", c_ubyte),
        ("share_mode", c_ubyte),
        ("is_submap", c_int),
        ("behavior", c_int),
        ("object_id", c_uint),
        ("user_wired_count", c_ushort),
        ("pages_reusable", c_uint),
        ("object_id_full", c_uint64),
    ]


VM_REGION_SUBMAP_INFO_COUNT_64 = ctypes.sizeof(VMRegionSubmapInfo64) // 4

VM_PROT_READ = 0x1
VM_PROT_WRITE = 0x2
VM_PROT_EXECUTE = 0x4

# share_mode values (mach/vm_region.h)
SM_COW = 1
SM_PRIVATE = 2
SM_EMPTY = 3
SM_SHARED = 4
SM_TRUESHARED = 5
SM_PRIVATE_ALIASED = 6
SM_SHARED_ALIASED = 7
SM_LARGE_PAGE = 8

KERN_SUCCESS = 0
KERN_INVALID_ADDRESS = 1
KERN_PROTECTION_FAILURE = 2
KERN_FAILURE = 5

# CGEvent constants
kCGHIDEventTap = 0
kCGEventSourceStateHIDSystemState = 1
kCGEventSourceStateCombinedSessionState = 0
kCGEventSourceStatePrivate = -1

kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventRightMouseDragged = 7
kCGEventOtherMouseDown = 25
kCGEventOtherMouseUp = 26
kCGEventOtherMouseDragged = 27

kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGMouseButtonCenter = 2

kCGMouseEventClickState = 1
kCGMouseEventDeltaX = 4
kCGMouseEventDeltaY = 5

kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListExcludeDesktopElements = 1 << 4

kCFNumberSInt64Type = 4
kCFNumberDoubleType = 13
kCFStringEncodingUTF8 = 0x08000100

_lib = None
_cf = None
_cg = None
_hi = None
_load_error = ""


def _load() -> None:
    global _lib, _cf, _cg, _hi, _load_error
    if _lib is not None or _load_error:
        return
    if not IS_MAC:
        _load_error = "not running on macOS"
        return
    try:
        _lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        _hi = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
    except OSError as exc:
        _lib = None
        _load_error = f"could not load system frameworks: {exc}"
        return

    # -- Mach ---------------------------------------------------------------
    _lib.task_for_pid.argtypes = [c_uint, c_int, POINTER(c_uint)]
    _lib.task_for_pid.restype = c_int
    _lib.mach_vm_read_overwrite.argtypes = [
        c_uint, c_uint64, c_uint64, c_uint64, POINTER(c_uint64)
    ]
    _lib.mach_vm_read_overwrite.restype = c_int
    _lib.mach_vm_region_recurse.argtypes = [
        c_uint, POINTER(c_uint64), POINTER(c_uint64), POINTER(c_uint), c_void_p, POINTER(c_uint)
    ]
    _lib.mach_vm_region_recurse.restype = c_int
    _lib.mach_error_string.argtypes = [c_int]
    _lib.mach_error_string.restype = c_char_p
    _lib.mach_port_deallocate.argtypes = [c_uint, c_uint]
    _lib.mach_port_deallocate.restype = c_int
    _lib.proc_pidpath.argtypes = [c_int, c_void_p, c_uint32]
    _lib.proc_pidpath.restype = c_int

    # -- CoreFoundation -----------------------------------------------------
    _cf.CFRelease.argtypes = [c_void_p]
    _cf.CFRelease.restype = None
    _cf.CFArrayGetCount.argtypes = [c_void_p]
    _cf.CFArrayGetCount.restype = c_long
    _cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
    _cf.CFArrayGetValueAtIndex.restype = c_void_p
    _cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
    _cf.CFDictionaryGetValue.restype = c_void_p
    _cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
    _cf.CFNumberGetValue.restype = c_bool
    _cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
    _cf.CFStringGetCString.restype = c_bool

    # -- CoreGraphics -------------------------------------------------------
    _cg.CGEventSourceCreate.argtypes = [c_int32]
    _cg.CGEventSourceCreate.restype = c_void_p
    _cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
    _cg.CGEventCreateKeyboardEvent.restype = c_void_p
    _cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
    _cg.CGEventCreateMouseEvent.restype = c_void_p
    _cg.CGEventCreate.argtypes = [c_void_p]
    _cg.CGEventCreate.restype = c_void_p
    _cg.CGEventGetLocation.argtypes = [c_void_p]
    _cg.CGEventGetLocation.restype = CGPoint
    _cg.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int64]
    _cg.CGEventSetIntegerValueField.restype = None
    _cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
    _cg.CGEventSetFlags.restype = None
    _cg.CGEventPost.argtypes = [c_uint32, c_void_p]
    _cg.CGEventPost.restype = None
    _cg.CGEventSourceKeyState.argtypes = [c_int32, c_uint16]
    _cg.CGEventSourceKeyState.restype = c_bool
    _cg.CGWindowListCopyWindowInfo.argtypes = [c_uint32, c_uint32]
    _cg.CGWindowListCopyWindowInfo.restype = c_void_p
    _cg.CGRectMakeWithDictionaryRepresentation.argtypes = [c_void_p, POINTER(CGRect)]
    _cg.CGRectMakeWithDictionaryRepresentation.restype = c_bool
    _cg.CGMainDisplayID.argtypes = []
    _cg.CGMainDisplayID.restype = c_uint32
    _cg.CGDisplayBounds.argtypes = [c_uint32]
    _cg.CGDisplayBounds.restype = CGRect
    for name in ("CGPreflightPostEventAccess", "CGRequestPostEventAccess",
                 "CGPreflightListenEventAccess", "CGRequestListenEventAccess"):
        fn = getattr(_cg, name, None)
        if fn is not None:
            fn.argtypes = []
            fn.restype = c_bool

    # -- HIServices ---------------------------------------------------------
    _hi.AXIsProcessTrusted.argtypes = []
    _hi.AXIsProcessTrusted.restype = c_bool


def available() -> bool:
    _load()
    return _lib is not None


def load_error() -> str:
    _load()
    return _load_error


def _need():
    _load()
    if _lib is None:
        raise NativeUnavailable(_load_error or "macOS frameworks unavailable")


# ---------------------------------------------------------------------------
# Mach
# ---------------------------------------------------------------------------


def mach_task_self() -> int:
    _need()
    return c_uint.in_dll(_lib, "mach_task_self_").value


def mach_error(kr: int) -> str:
    _need()
    try:
        text = _lib.mach_error_string(kr)
        return text.decode("utf-8", "replace") if text else f"kern_return {kr}"
    except Exception:
        return f"kern_return {kr}"


def task_for_pid(pid: int) -> tuple[int, int]:
    """(kern_return, task_port). task_port is 0 unless kern_return is 0."""
    _need()
    port = c_uint(0)
    kr = _lib.task_for_pid(mach_task_self(), int(pid), byref(port))
    return kr, (port.value if kr == KERN_SUCCESS else 0)


def deallocate_port(port: int) -> None:
    if _lib is not None and port:
        try:
            _lib.mach_port_deallocate(mach_task_self(), port)
        except Exception:
            pass


def vm_read_into(task: int, address: int, size: int, buf_addr: int, out_size) -> int:
    """mach_vm_read_overwrite; `out_size` is a c_uint64 the caller reuses."""
    return _lib.mach_vm_read_overwrite(task, address, size, buf_addr, byref(out_size))


def vm_regions(task: int):
    """Yield (start, size, VMRegionSubmapInfo64) for every leaf region of a task."""
    _need()
    address = c_uint64(0)
    size = c_uint64(0)
    depth = c_uint(0)
    info = VMRegionSubmapInfo64()
    count = c_uint(VM_REGION_SUBMAP_INFO_COUNT_64)
    guard = 0
    while guard < 500000:
        guard += 1
        count.value = VM_REGION_SUBMAP_INFO_COUNT_64
        kr = _lib.mach_vm_region_recurse(
            task, byref(address), byref(size), byref(depth), byref(info), byref(count)
        )
        if kr != KERN_SUCCESS:
            return
        if info.is_submap:
            depth.value += 1
            continue
        yield address.value, size.value, info
        address.value += size.value


def proc_path(pid: int) -> str:
    _need()
    buf = ctypes.create_string_buffer(4096)
    n = _lib.proc_pidpath(int(pid), buf, 4096)
    return buf.value.decode("utf-8", "replace") if n > 0 else ""


# ---------------------------------------------------------------------------
# CoreFoundation helpers
# ---------------------------------------------------------------------------


def _cg_const(name: str) -> int:
    return c_void_p.in_dll(_cg, name).value


def _cf_number(ref: int, kind: int = kCFNumberSInt64Type):
    if not ref:
        return None
    if kind == kCFNumberDoubleType:
        out = c_double(0)
    else:
        out = c_int64(0)
    if not _cf.CFNumberGetValue(ref, kind, byref(out)):
        return None
    return out.value


def _cf_string(ref: int) -> str:
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(1024)
    if _cf.CFStringGetCString(ref, buf, 1024, kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def list_windows(on_screen_only: bool = True) -> list[dict]:
    """The window server's window list, front to back.

    Each entry: {pid, layer, owner, name, x, y, w, h}. Window titles need the
    Screen Recording permission and come back empty without it; nothing here
    depends on them -- windows are matched by owner pid.
    """
    _need()
    opts = kCGWindowListExcludeDesktopElements
    if on_screen_only:
        opts |= kCGWindowListOptionOnScreenOnly
    arr = _cg.CGWindowListCopyWindowInfo(opts, 0)
    if not arr:
        return []
    k_pid = _cg_const("kCGWindowOwnerPID")
    k_layer = _cg_const("kCGWindowLayer")
    k_bounds = _cg_const("kCGWindowBounds")
    k_owner = _cg_const("kCGWindowOwnerName")
    k_name = _cg_const("kCGWindowName")
    out = []
    try:
        for i in range(_cf.CFArrayGetCount(arr)):
            d = _cf.CFArrayGetValueAtIndex(arr, i)
            if not d:
                continue
            rect = CGRect()
            bounds = _cf.CFDictionaryGetValue(d, k_bounds)
            if not bounds or not _cg.CGRectMakeWithDictionaryRepresentation(bounds, byref(rect)):
                continue
            out.append({
                "pid": _cf_number(_cf.CFDictionaryGetValue(d, k_pid)) or 0,
                "layer": _cf_number(_cf.CFDictionaryGetValue(d, k_layer)) or 0,
                "owner": _cf_string(_cf.CFDictionaryGetValue(d, k_owner)),
                "name": _cf_string(_cf.CFDictionaryGetValue(d, k_name)),
                "x": rect.origin.x,
                "y": rect.origin.y,
                "w": rect.size.width,
                "h": rect.size.height,
            })
    finally:
        _cf.CFRelease(arr)
    return out


def main_display_bounds() -> tuple[float, float, float, float]:
    _need()
    r = _cg.CGDisplayBounds(_cg.CGMainDisplayID())
    return r.origin.x, r.origin.y, r.size.width, r.size.height


# ---------------------------------------------------------------------------
# CGEvent
# ---------------------------------------------------------------------------


def event_source():
    """A private event source, so our synthetic events never feed the hotkey poll."""
    _need()
    return _cg.CGEventSourceCreate(kCGEventSourceStatePrivate)


def post_key(source, keycode: int, down: bool) -> None:
    ev = _cg.CGEventCreateKeyboardEvent(source, keycode, down)
    if not ev:
        raise NativeUnavailable("CGEventCreateKeyboardEvent returned NULL")
    try:
        _cg.CGEventSetFlags(ev, 0)  # never inherit a held Cmd/Shift from the user
        _cg.CGEventPost(kCGHIDEventTap, ev)
    finally:
        _cf.CFRelease(ev)


def post_mouse(source, ev_type: int, x: float, y: float, button: int,
               click_state: int = 0, dx: int = 0, dy: int = 0) -> None:
    ev = _cg.CGEventCreateMouseEvent(source, ev_type, CGPoint(x, y), button)
    if not ev:
        raise NativeUnavailable("CGEventCreateMouseEvent returned NULL")
    try:
        if click_state:
            _cg.CGEventSetIntegerValueField(ev, kCGMouseEventClickState, click_state)
        if dx or dy:
            _cg.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaX, int(dx))
            _cg.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaY, int(dy))
        _cg.CGEventPost(kCGHIDEventTap, ev)
    finally:
        _cf.CFRelease(ev)


def cursor_position() -> tuple[float, float]:
    _need()
    ev = _cg.CGEventCreate(None)
    if not ev:
        return 0.0, 0.0
    try:
        p = _cg.CGEventGetLocation(ev)
        return p.x, p.y
    finally:
        _cf.CFRelease(ev)


def key_is_down(keycode: int) -> bool:
    """Is a physical key held right now? (hardware state, not synthetic events)"""
    return bool(_cg.CGEventSourceKeyState(kCGEventSourceStateHIDSystemState, keycode))


# ---------------------------------------------------------------------------
# Permissions (TCC)
# ---------------------------------------------------------------------------


def _call_bool(lib, name: str):
    fn = getattr(lib, name, None) if lib is not None else None
    if fn is None:
        return None
    try:
        return bool(fn())
    except Exception:
        return None


def accessibility_trusted():
    """Accessibility permission (needed to send clicks/keys). None = unknown."""
    _load()
    return _call_bool(_hi, "AXIsProcessTrusted")


def post_event_access():
    _load()
    return _call_bool(_cg, "CGPreflightPostEventAccess")


def listen_event_access():
    """Input Monitoring permission (needed for the global hotkeys)."""
    _load()
    return _call_bool(_cg, "CGPreflightListenEventAccess")


def request_permissions() -> None:
    """Ask macOS to show its permission prompts (adds Terminal to the lists)."""
    _load()
    _call_bool(_cg, "CGRequestPostEventAccess")
    _call_bool(_cg, "CGRequestListenEventAccess")
