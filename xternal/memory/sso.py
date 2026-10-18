"""
XTernal for Mac -- libc++ std::string layouts.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

The macOS client is built with Clang against libc++, like the Android build the
Linux port reads. libc++ has two little-endian string layouts, and which one a
binary uses is a build-time ABI choice, so both are supported and the
calibrator records whichever one the live client actually uses:

  standard (STRING_ABI_LIBCXX)
      short: byte 0 = len << 1 (low bit clear), chars from byte 1 (max 22)
      long:  byte 0 low bit set; size at +0x08, data pointer at +0x10

  alternate (STRING_ABI_LIBCXX_ALT, _LIBCPP_ABI_ALTERNATE_STRING_LAYOUT)
      short: chars from byte 0, byte 23 = len (high bit clear)
      long:  byte 23 high bit set; data pointer at +0x00, size at +0x08
"""

from __future__ import annotations

import struct

from .offsets import STRING_ABI_LIBCXX, STRING_ABI_LIBCXX_ALT

MAX_SHORT = 22


def sso_image(text: str, abi: str) -> bytes:
    """Bytes that must appear for a short string holding `text` (search needle).

    For the alternate layout this is only the leading part (chars + NUL); the
    length byte sits at +23 and the bytes in between are not guaranteed to be
    zero, so callers check it separately with sso_decode().
    """
    raw = text.encode()
    if abi == STRING_ABI_LIBCXX_ALT:
        return raw + b"\x00"
    return bytes([len(raw) << 1]) + raw + b"\x00"


def sso_decode(head: bytes, abi: str):
    """Decode a short string from a 24-byte head.

    Returns the text, or None when the head is a long string or not a string.
    An empty short string decodes as "".
    """
    if head is None or len(head) < 24:
        return None
    if abi == STRING_ABI_LIBCXX_ALT:
        last = head[23]
        if last & 0x80:
            return None
        length = last
        if length > MAX_SHORT:
            return None
        if head[length] != 0:  # the NUL terminator must be where the length says
            return None
        return head[:length].decode("utf-8", "replace")
    first = head[0]
    if first & 1:
        return None
    length = first >> 1
    if length > MAX_SHORT:
        return None
    return head[1 : 1 + length].decode("utf-8", "replace")


def long_fields(head: bytes, abi: str):
    """(length, data_pointer) for a long string, or None if it is short."""
    if head is None or len(head) < 24:
        return None
    if abi == STRING_ABI_LIBCXX_ALT:
        if not head[23] & 0x80:
            return None
        data, length = struct.unpack_from("<QQ", head, 0)
        return length, data
    if not head[0] & 1:
        return None
    length, data = struct.unpack_from("<QQ", head, 8)
    return length, data


ALL_LIBCXX = (STRING_ABI_LIBCXX, STRING_ABI_LIBCXX_ALT)
