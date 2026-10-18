"""
XTernal for Mac -- logging.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

The log is the support channel: whoever runs this sends the file (or the zip
from `xternal-mac report`) to whoever maintains it. So it is written to be read
by someone who was not there:

  * every session starts with a header block -- macOS version, CPU, Python,
    Roblox version, root/SIP/permission state -- so "what machine is this" is
    never a question;
  * DEBUG and up goes to ~/Library/Logs/xternal-mac/xternal.log (rotated,
    2 MB x 5); INFO and up also goes to the terminal;
  * uncaught exceptions on any thread are logged with their traceback instead
    of vanishing;
  * the same error repeating every tick is logged once in full, then counted
    (`log_once`), so one fault cannot bury everything else.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

_configured = False
_listeners: list = []
_once: dict[str, list] = {}
_once_lock = threading.Lock()

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Pass as extra= to keep a record out of the terminal (it still reaches the
#: log file and the in-app Log tab). For bulky blocks the user doesn't need.
FILE_ONLY = {"file_only": True}


class _ListenerHandler(logging.Handler):
    """Fans formatted records out to in-app viewers (the GUI's Log tab)."""

    def emit(self, record: logging.LogRecord) -> None:
        if not _listeners:
            return
        try:
            line = self.format(record)
        except Exception:
            return
        for fn in list(_listeners):
            try:
                fn(record.levelno, line)
            except Exception:
                pass


def add_listener(fn) -> None:
    _listeners.append(fn)


def remove_listener(fn) -> None:
    try:
        _listeners.remove(fn)
    except ValueError:
        pass


def get_logger(name: str = "xternal") -> logging.Logger:
    setup()
    return logging.getLogger(name if name.startswith("xternal") else f"xternal.{name}")


def setup(console: bool = True) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    from .settings import ensure_dirs, give_back, log_path

    root = logging.getLogger("xternal")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    fmt = logging.Formatter(FORMAT, DATEFMT)

    try:
        ensure_dirs()
        handler = RotatingFileHandler(
            log_path(), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)
        give_back(log_path())
    except OSError as exc:
        print(f"[xternal] could not open log file: {exc}", file=sys.stderr)

    if console:
        con = logging.StreamHandler(sys.stderr)
        con.setLevel(logging.INFO)
        con.addFilter(lambda record: not getattr(record, "file_only", False))
        con.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        root.addHandler(con)

    lh = _ListenerHandler()
    lh.setLevel(logging.DEBUG)
    lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    root.addHandler(lh)

    _install_crash_hooks(root)


def _install_crash_hooks(log: logging.Logger) -> None:
    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical(
            "UNCAUGHT EXCEPTION (main thread)\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "UNCAUGHT EXCEPTION in thread %s\n%s",
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def log_once(log: logging.Logger, key: str, message: str, *args, exc_info=False,
             level=logging.ERROR, every_s: float = 60.0) -> None:
    """Log a repeating problem in full once, then a count at most every `every_s`.

    For errors raised from per-tick code: the first occurrence carries the
    traceback; after that the log only says how many more happened.
    """
    now = time.monotonic()
    with _once_lock:
        entry = _once.get(key)
        if entry is None:
            _once[key] = [now, 0]
            first = True
        else:
            entry[1] += 1
            first = False
            if now - entry[0] < every_s:
                return
            count = entry[1]
            entry[0], entry[1] = now, 0
    if first:
        log.log(level, message, *args, exc_info=exc_info)
    else:
        log.log(level, "(repeated %d more times in the last %.0fs) " + message,
                count, every_s, *args)


def write_session_header(log: logging.Logger, context: str) -> None:
    """The block at the top of every session: what machine, what state."""
    from .diagnostics import collect_system_info

    quiet = {"extra": FILE_ONLY}
    log.info("=" * 72, **quiet)
    log.info("XTernal for Mac session start (%s)", context, **quiet)
    try:
        for key, value in collect_system_info().items():
            log.info("  %-22s %s", key + ":", value, **quiet)
    except Exception:
        log.exception("could not collect system info")
    log.info("=" * 72, **quiet)
