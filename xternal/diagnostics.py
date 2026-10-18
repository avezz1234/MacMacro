"""
XTernal for Mac -- system info, the doctor checklist, and support reports.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Three things live here, all aimed at "it doesn't work, what do I send you?":

  collect_system_info()  key facts about the machine, logged every session
  run_doctor()           a checklist; each failure carries the exact fix
  make_report()          one zip on the Desktop with everything needed
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import settings as cfg
from .macos import native

OK, WARN, FAIL = "ok", "warn", "fail"


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _yes_no(value) -> str:
    return {True: "yes", False: "NO", None: "unknown"}[value]


def roblox_bundle_info(exe_path: str = "") -> dict:
    """Version and architectures of the installed Roblox player."""
    app = ""
    if exe_path and ".app/" in exe_path:
        app = exe_path[: exe_path.index(".app/") + 4]
    elif os.path.isdir("/Applications/Roblox.app"):
        app = "/Applications/Roblox.app"
    else:
        user_app = cfg.real_home() / "Applications" / "Roblox.app"
        if user_app.is_dir():
            app = str(user_app)
    info = {"app": app or "(not found)", "version": "", "archs": ""}
    if not app:
        return info
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
            plist = plistlib.load(fh)
        info["version"] = str(plist.get("CFBundleShortVersionString", "")) + (
            f" ({plist.get('CFBundleVersion')})" if plist.get("CFBundleVersion") else ""
        )
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    exe = exe_path or os.path.join(app, "Contents", "MacOS", "RobloxPlayer")
    info["archs"] = _run(["/usr/bin/lipo", "-archs", exe])
    return info


def offsets_summary() -> str:
    path = cfg.offsets_path()
    if not path.exists():
        return "not calibrated"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable ({exc})"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    return (f"{len(data.get('offsets', {}))} fields, abi={data.get('string_abi')}, "
            f"roblox={data.get('version') or '?'}, calibrated {when}")


def collect_system_info() -> dict:
    from .memory.proc import find_client, sip_status_text

    mac_ver = platform.mac_ver()[0] or "?"
    build = _run(["/usr/bin/sw_vers", "-buildVersion"])
    translated = _run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"])
    info = {
        "app": cfg.FULL_VER,
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "macOS": f"{mac_ver} ({build})" if build else mac_ver,
        "cpu": _run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "machine": platform.machine() + (" (Python under Rosetta)" if translated == "1" else ""),
        "python": f"{platform.python_version()} at {sys.executable}",
    }
    try:
        import PySide6

        info["PySide6"] = PySide6.__version__
    except Exception as exc:
        info["PySide6"] = f"MISSING ({exc})"
    try:
        import numpy

        info["numpy"] = numpy.__version__
    except Exception:
        info["numpy"] = "missing (calibration will be slower)"
    user, uid, _gid, home = cfg.real_user()
    info["user"] = f"{user} (uid {uid}), running as {'root' if os.geteuid() == 0 else 'normal user'}"
    sip = sip_status_text().replace("\n", " | ").replace("\t", " ")
    info["SIP"] = " ".join(sip.split()) or "unknown"
    info["native frameworks"] = "loaded" if native.available() else f"NOT loaded: {native.load_error()}"
    if native.available():
        info["accessibility"] = _yes_no(native.accessibility_trusted())
        info["post events"] = _yes_no(native.post_event_access())
        info["input monitoring"] = _yes_no(native.listen_event_access())
        try:
            x, y, w, h = native.main_display_bounds()
            info["main display"] = f"{w:.0f}x{h:.0f} pt at ({x:.0f},{y:.0f})"
        except Exception:
            pass
    client = None
    try:
        client = find_client()
    except Exception:
        pass
    rb = roblox_bundle_info(client.detail if client else "")
    info["roblox app"] = f"{rb['app']} version {rb['version'] or '?'} [{rb['archs'] or '?'}]"
    info["roblox running"] = f"pid {client.pid}" if client else "no"
    info["offsets"] = offsets_summary()
    info["config dir"] = str(cfg.config_dir())
    info["log file"] = str(cfg.log_path())
    return info


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""
    fix: str = ""


SIP_FIX = (
    "Roblox can't be read while SIP's debugging protection is on. One-time fix:\n"
    "  1. Shut down the Mac.\n"
    "  2. Apple Silicon (M1/M2/M3/M4): hold the power button until 'Loading startup\n"
    "     options' appears, click Options > Continue.\n"
    "     Intel Mac: power on while holding Cmd+R.\n"
    "  3. In the menu bar pick Utilities > Terminal and type:\n"
    "         csrutil enable --without debug\n"
    "     then press Enter, type 'y' if asked, and restart (Apple menu > Restart).\n"
    "  To undo later: same steps, but type   csrutil enable"
)

ACCESS_FIX = (
    "System Settings > Privacy & Security > Accessibility: turn ON 'Terminal'\n"
    "  (click + and add /Applications/Utilities/Terminal.app if it isn't listed).\n"
    "  Then quit Terminal completely (Cmd+Q) and start the macro again."
)

INPUT_MON_FIX = (
    "System Settings > Privacy & Security > Input Monitoring: turn ON 'Terminal',\n"
    "  then quit Terminal (Cmd+Q) and reopen. Without it, F1-F5 hotkeys won't\n"
    "  work -- the buttons in the macro window still do."
)


def run_doctor() -> list[Check]:
    from .memory.proc import MemoryAccessError, ProcessMemory, find_client, sip_allows_debugging

    checks: list[Check] = []

    if sys.platform != "darwin":
        checks.append(Check(FAIL, "macOS", f"this is {sys.platform}", "This build only runs on a Mac."))
        return checks
    checks.append(Check(OK, "macOS", platform.mac_ver()[0]))

    if sys.version_info < (3, 10):
        checks.append(Check(FAIL, "Python", platform.python_version(),
                            "Python 3.10 or newer is needed. Run XTernal.command again (it repairs the install)."))
    else:
        checks.append(Check(OK, "Python", platform.python_version()))

    try:
        import PySide6  # noqa: F401

        checks.append(Check(OK, "PySide6 (window toolkit)", PySide6.__version__))
    except Exception as exc:
        checks.append(Check(FAIL, "PySide6 (window toolkit)", str(exc), "Run XTernal.command again (it repairs the install)."))
    try:
        import numpy  # noqa: F401

        checks.append(Check(OK, "numpy", numpy.__version__))
    except Exception:
        checks.append(Check(WARN, "numpy", "missing", "Calibration will be slow. Run XTernal.command again (it repairs the install)."))

    if not native.available():
        checks.append(Check(FAIL, "system frameworks", native.load_error(),
                            "Unexpected -- send the report zip."))
        return checks

    if os.geteuid() == 0:
        checks.append(Check(OK, "running as root", "needed to read Roblox's memory"))
    else:
        checks.append(Check(FAIL, "running as root", "no",
                            "Start it with XTernal.command (asks for your password)."))

    sip = sip_allows_debugging()
    if sip is True:
        checks.append(Check(OK, "SIP debugging restrictions", "off"))
    elif sip is False:
        checks.append(Check(FAIL, "SIP debugging restrictions", "ON (blocks reading Roblox)", SIP_FIX))
    else:
        checks.append(Check(WARN, "SIP debugging restrictions", "could not tell", SIP_FIX))

    ax = native.accessibility_trusted()
    post = native.post_event_access()
    if ax or post:
        checks.append(Check(OK, "Accessibility (clicks/keys)", "allowed"))
    else:
        checks.append(Check(FAIL, "Accessibility (clicks/keys)", "not allowed", ACCESS_FIX))

    listen = native.listen_event_access()
    if listen is False:
        checks.append(Check(WARN, "Input Monitoring (hotkeys)", "not allowed", INPUT_MON_FIX))
    else:
        checks.append(Check(OK, "Input Monitoring (hotkeys)", "allowed" if listen else "unknown"))

    client = find_client()
    if client is None:
        checks.append(Check(WARN, "Roblox", "not running", "Open Roblox and join Fisch, then check again."))
    else:
        rb = roblox_bundle_info(client.detail)
        checks.append(Check(OK, "Roblox", f"pid {client.pid}, version {rb['version'] or '?'}"))
        try:
            mem = ProcessMemory(client.pid)
            summary = mem.region_summary()
            regions = mem.scannable_regions()
            probe = mem.read(regions[0].start, 8) if regions else None
            if probe is None:
                checks.append(Check(FAIL, "read Roblox memory", "opened, but reads fail",
                                    "Restart Roblox and try again; send the report if it persists."))
            else:
                checks.append(Check(OK, "read Roblox memory", summary))
        except MemoryAccessError as exc:
            checks.append(Check(FAIL, "read Roblox memory", "denied", str(exc)))
        except Exception as exc:
            checks.append(Check(FAIL, "read Roblox memory", repr(exc), "Send the report zip."))

    path = cfg.offsets_path()
    if not path.exists():
        checks.append(Check(WARN, "offsets", "not calibrated yet",
                            "Join a Fisch server, then press Calibrate in the macro window."))
    else:
        summary = offsets_summary()
        stale = ""
        if client is not None:
            try:
                saved = json.loads(path.read_text(encoding="utf-8")).get("version", "")
                current = roblox_bundle_info(client.detail)["version"]
                if saved and current and saved != current:
                    stale = f"Roblox updated ({saved} -> {current}); press Calibrate again in Fisch."
            except (OSError, ValueError):
                pass
        checks.append(Check(WARN if stale else OK, "offsets", summary, stale))
    return checks


def format_doctor(checks: list[Check], color: bool = True) -> str:
    tags = {
        OK: "\033[32m  ok  \033[0m" if color else "  ok  ",
        WARN: "\033[33m warn \033[0m" if color else " warn ",
        FAIL: "\033[31m FAIL \033[0m" if color else " FAIL ",
    }
    lines = []
    for c in checks:
        lines.append(f"[{tags[c.status]}] {c.label}" + (f" -- {c.detail}" if c.detail else ""))
        if c.fix and c.status != OK:
            lines.extend("         " + line for line in c.fix.splitlines())
    problems = sum(c.status == FAIL for c in checks)
    lines.append("")
    lines.append("All good." if problems == 0 else f"{problems} problem(s) need fixing (see above).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _redacted_settings() -> str:
    try:
        data = json.loads(cfg.settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"(unreadable: {exc})"
    hook = data.get("webhook", {})
    if hook.get("webhook_url"):
        hook["webhook_url"] = "<redacted>"
    return json.dumps(data, indent=2)


def make_report() -> Path:
    """Zip logs + doctor + system info + settings (webhook redacted) to the Desktop."""
    home = cfg.real_home()
    desktop = home / "Desktop"
    if not desktop.is_dir():
        desktop = home
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = desktop / f"xternal-report-{stamp}.zip"

    try:
        doctor_text = format_doctor(run_doctor(), color=False)
    except Exception as exc:
        doctor_text = f"doctor crashed: {exc!r}"
    try:
        sysinfo = "\n".join(f"{k}: {v}" for k, v in collect_system_info().items())
    except Exception as exc:
        sysinfo = f"system info crashed: {exc!r}"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doctor.txt", doctor_text)
        z.writestr("system.txt", sysinfo)
        z.writestr("settings.json", _redacted_settings())
        z.writestr("windows.txt", _window_dump())
        for p in sorted(cfg.log_dir().glob("xternal.log*")):
            z.write(p, f"logs/{p.name}")
        if cfg.offsets_path().exists():
            z.write(cfg.offsets_path(), cfg.offsets_path().name)
        feed = cfg.catch_feed_path()
        if feed.exists():
            lines = feed.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            z.writestr("catches-tail.jsonl", "\n".join(lines))
        z.writestr("configs.txt", "\n".join(cfg.list_configs()) or "(none)")
    cfg.give_back(out)
    return out


def _window_dump() -> str:
    """On-screen windows (owner + bounds), to debug click placement."""
    if not native.available():
        return "unavailable"
    try:
        rows = native.list_windows()
    except Exception as exc:
        return f"failed: {exc!r}"
    return "\n".join(
        f"pid={w['pid']:<6} layer={w['layer']:<3} {w['owner'][:28]:<28} "
        f"{w['w']:.0f}x{w['h']:.0f} at ({w['x']:.0f},{w['y']:.0f})"
        for w in rows
    )


def reveal_in_finder(path: Path) -> None:
    """Show a file in Finder, as the real user (not root)."""
    user = cfg.real_user()[0]
    cmd = ["/usr/bin/open", "-R", str(path)]
    if os.geteuid() == 0 and user and user != "root":
        cmd = ["/usr/bin/sudo", "-u", user] + cmd
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
