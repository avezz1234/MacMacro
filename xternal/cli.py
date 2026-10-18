"""
XTernal for Mac -- command line entry point.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Subcommands:
    (none)/gui  launch the window
    doctor      check everything and say exactly what to fix
    calibrate   work out the offsets for this Roblox build (be in Fisch)
    dump        print live game state (debugging aid)
    report      zip logs + diagnostics onto the Desktop to send for help
    permissions show macOS's Accessibility / Input Monitoring prompts
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys

from . import settings as cfg
from .log import FILE_ONLY, get_logger, setup, write_session_header

NEEDS_ROOT = {"gui", "doctor", "calibrate", "dump", "report"}


def _elevate(command: str) -> None:
    """Re-run this exact command under sudo (reading Roblox requires root)."""
    if os.geteuid() == 0 or command not in NEEDS_ROOT or sys.platform != "darwin":
        return
    if os.environ.get("XTERNAL_NO_SUDO"):
        return
    if not os.environ.get("XTERNAL_QUIET_SUDO"):
        print("XTernal needs your Mac login password to read Roblox's memory.")
        print("(Nothing shows while you type the password -- that's normal. Press Enter after.)\n")
    argv = ["/usr/bin/sudo", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        print(f"Could not run sudo: {exc}")
        sys.exit(1)


def cmd_doctor(_args) -> int:
    from .diagnostics import FAIL, format_doctor, run_doctor

    print("XTernal for Mac -- checkup\n")
    checks = run_doctor()
    text = format_doctor(checks, color=sys.stdout.isatty())
    print(text)
    log = get_logger()
    log.info("doctor results:\n%s", format_doctor(checks, color=False), extra=FILE_ONLY)
    print(f"\nLog file: {cfg.log_path()}")
    return 1 if any(c.status == FAIL for c in checks) else 0


def cmd_calibrate(args) -> int:
    from .diagnostics import roblox_bundle_info
    from .memory.calibrate import calibrate
    from .memory.offsets import load_calibrated_offsets, save_calibrated_offsets
    from .memory.proc import MemoryAccessError, ProcessMemory, find_client

    log = get_logger()
    client = find_client()
    if client is None:
        print("Roblox is not running. Open Roblox and join a Fisch server first.")
        return 1

    if args.dynamic:
        return _cmd_calibrate_dynamic(args, client)

    print(f"Calibrating against {client}.")
    print("Reads memory only; takes a few seconds to a minute.\n")
    try:
        mem = ProcessMemory(client.pid)
    except MemoryAccessError as exc:
        print(exc)
        log.error("calibrate: %s", exc)
        return 1
    log.info("calibrate: %s -- %s", client, mem.region_summary())

    def progress(msg: str) -> None:
        print(f"  {msg}")
        log.info("calibrate: %s", msg)

    result = calibrate(mem, place_id=cfg.FISCH_PLACE_ID, progress=progress)
    print()
    for step in result.steps:
        print(f"  {step}")
        log.info("calibrate step: %s", step)
    print()

    if not result.ok:
        print(f"Calibration failed: {result.error}")
        print("Make sure you are loaded into a Fisch server (not the menu), then try again.")
        print("If it keeps failing, run ./xternal-mac report and send the zip.")
        log.error("calibrate failed: %s", result.error)
        return 1

    result.offsets.build_version = roblox_bundle_info(client.detail)["version"]
    path = args.output or cfg.offsets_path()
    save_calibrated_offsets(result.offsets, path)
    log.info("calibrate: saved %d offsets to %s:\n%s", len(result.offsets.table), path,
             result.offsets.to_json(), extra=FILE_ONLY)
    print(f"Saved offsets to {path}")

    saved = load_calibrated_offsets(path)
    missing = [k for k in ("ScreenGuiEnabled", "FrameVisible", "FramePositionX", "Value")
               if not saved.has(k)]
    if missing:
        print("\nNot resolved: " + ", ".join(missing))
        print("Usually fine. For Tranquility/Lullaby rods, also run:  ./xternal-mac calibrate --dynamic")
    return 0


def _cmd_calibrate_dynamic(args, client) -> int:
    from .logic.game import GameLink
    from .memory.dynamic import calibrate_dynamic
    from .memory.offsets import load_calibrated_offsets, save_calibrated_offsets

    log = get_logger()
    path = args.output or cfg.offsets_path()
    if not os.path.exists(path):
        print(f"No offsets at {path}. Run the normal calibration first.")
        return 1

    link = GameLink()
    try:
        link.attach(progress=lambda s: print(f"  {s}"))
    except Exception as exc:
        print(f"Attach failed: {exc}")
        log.error("dynamic calibrate: attach failed: %s", exc)
        return 1

    print("\nStart fishing now -- cast and reel as you normally would.\n")
    found = calibrate_dynamic(
        link.mem, link.offsets, link.reader.get_data_model(),
        seconds=args.seconds, progress=lambda s: print(f"  {s}"),
    )
    if not found:
        print("\nNothing could be pinned. Reel at least one fish while it watches.")
        log.warning("dynamic calibrate: nothing pinned")
        return 1

    candidates = found.pop("_candidates", [])
    offsets = load_calibrated_offsets(path)
    offsets.table.update(found)
    offsets.table.setdefault("FrameVisible", found["ScreenGuiEnabled"])
    offsets.table.setdefault("TextLabelVisible", found["ScreenGuiEnabled"])
    save_calibrated_offsets(offsets, path)
    log.info("dynamic calibrate: ScreenGuiEnabled=%s candidates=%s",
             hex(found["ScreenGuiEnabled"]), [hex(c) for c in candidates[:8]])
    print(f"\nScreenGuiEnabled = {hex(found['ScreenGuiEnabled'])}")
    print(f"Saved to {path}")
    return 0


def cmd_dump(_args) -> int:
    from .logic.game import FischGame, GameLink

    link = GameLink()
    try:
        link.attach(progress=lambda s: print(f"  {s}", file=sys.stderr))
    except Exception as exc:
        print(f"Attach failed: {exc}")
        return 1

    r = link.reader
    game = FischGame(link)
    lines = [
        f"client      : {link.client}",
        f"DataModel   : {hex(r.get_data_model())}",
        f"PlaceId     : {r.try_get_place_id()} (Fisch = {cfg.FISCH_PLACE_ID})",
        f"LocalPlayer : {r.read_instance_name(r.get_local_player())}",
        f"Rod         : {game.get_hotbar_rod_name()!r}",
        f"Equipped    : {game.get_equipped_tool_name()!r}",
        f"Cycle       : {game.get_current_cycle()!r}",
        f"Weather     : {game.get_current_weather()!r}",
        f"Reel GUI    : {hex(game.get_reel_gui())}",
        f"Reel visible: {game.is_reel_gui_visible()}",
        f"Progress    : {game.get_fishing_completion_percent()}",
        "Hotbar:",
    ]
    for slot in game._hotbar_slots():
        lines.append(f"  [{game.read_hotbar_item_slot_key(slot)}] {game.read_hotbar_item_name(slot)!r}")
    text = "\n".join(lines)
    print(text)
    get_logger().info("dump:\n%s", text, extra=FILE_ONLY)
    return 0


def cmd_report(_args) -> int:
    from .diagnostics import make_report, reveal_in_finder

    print("Collecting logs and diagnostics...")
    path = make_report()
    get_logger().info("report written to %s", path)
    print(f"\nSaved: {path}")
    print("Send that zip file to whoever is helping you.")
    reveal_in_finder(path)
    return 0


def cmd_permissions(_args) -> int:
    from .macos import native

    if not native.available():
        print(f"Unavailable: {native.load_error()}")
        return 1
    native.request_permissions()
    print("If macOS showed prompts, click 'Open System Settings' and switch ON 'Terminal' in:")
    print("  - Privacy & Security > Accessibility")
    print("  - Privacy & Security > Input Monitoring")
    print("Then quit Terminal completely (Cmd+Q) and open it again.")
    return 0


def cmd_preflight(_args) -> int:
    from .preflight import run

    return run()


def cmd_gui(_args) -> int:
    try:
        from .ui.app import run
    except ImportError as exc:
        print(f"The window needs PySide6 ({exc}). Run XTernal.command again (it repairs the install).")
        get_logger().error("PySide6 import failed: %s", exc)
        return 1
    return run()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xternal-mac",
        description="XTernal Fisch macro, macOS port (AGPL-3.0-only).",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="open the macro window (default)").set_defaults(func=cmd_gui)
    sub.add_parser("doctor", help="check setup and explain fixes").set_defaults(func=cmd_doctor)
    p_cal = sub.add_parser("calibrate", help="find offsets for this Roblox build (be in Fisch)")
    p_cal.add_argument("-o", "--output", help="where to write the offsets file")
    p_cal.add_argument("--dynamic", action="store_true",
                       help="second pass: watch a real reel to pin the Visible/Enabled flags")
    p_cal.add_argument("--seconds", type=int, default=90, help="dynamic pass duration")
    p_cal.set_defaults(func=cmd_calibrate)
    sub.add_parser("dump", help="print live game state").set_defaults(func=cmd_dump)
    sub.add_parser("report", help="make a support zip on the Desktop").set_defaults(func=cmd_report)
    sub.add_parser("permissions", help="trigger the macOS permission prompts").set_defaults(
        func=cmd_permissions)
    sub.add_parser("preflight", help="walk through SIP/permission setup (used by XTernal.command)"
                   ).set_defaults(func=cmd_preflight)

    args = parser.parse_args(argv)
    command = args.command or "gui"
    _elevate(command)

    setup(console=True)
    atexit.register(cfg.fix_ownership)
    log = get_logger()
    write_session_header(log, command)
    try:
        code = getattr(args, "func", cmd_gui)(args)
    except KeyboardInterrupt:
        log.info("interrupted")
        code = 130
    except Exception:
        log.exception("command %r crashed", command)
        print(f"\nSomething crashed. Details are in {cfg.log_path()}")
        print("Run ./xternal-mac report and send the zip.")
        code = 1
    log.info("exit %s (code %s)", command, code, extra=FILE_ONLY)
    return code


if __name__ == "__main__":
    sys.exit(main())
