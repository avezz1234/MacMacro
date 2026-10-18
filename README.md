# XTernal for Mac

A macOS port of **OpenMacro XTernal**, the memory-reading auto-fishing macro for
the Roblox game *Fisch*. It reads the game's state straight from memory (no
screen scraping), so window size, resolution and UI scale don't matter.

Original by misery ([@termx3](https://github.com/termx3)), maintained by
shinkting ([@Invermatic1](https://github.com/Invermatic1)), copyright © 2026
(@anorexc). Licensed **AGPL-3.0-only**. This port is a modified version
distributed under the same licence; see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). Upstream: <https://discord.gg/openmacro>

> **Heads up:** this is a third-party macro. Using one can get a Roblox account
> banned. It also requires relaxing one macOS security setting (the installer
> walks you through it). Use an account you're OK with risking.

---

## Install and run: one command

1. Open **Terminal**: press **Cmd + Space**, type `terminal`, press **Enter**.
2. Copy this line, paste it into Terminal (**Cmd + V**), press **Enter**:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/avezz1234/MacMacro/main/get.sh | bash
   ```

3. **Do whatever the yellow or red boxes say.** It only stops when it needs you
   to do something, tells you exactly what to click, and carries on by itself
   once you've done it.

That's it. **After the first time, double-click `XTernal` on your Desktop.**
(Pasting the line again also works, and updates to the newest version.)

### What it will ask you to do (first time only)

- **Switch on "Terminal"** in two Settings pages that it opens for you
  (*Accessibility* and *Input Monitoring*).
- **Unlock Roblox's memory.** This is a one-time restart into the Mac's recovery
  screen to type one command. It shows the steps for your exact Mac. **Take a
  photo of them with your phone** before you start, because you won't see this
  window while doing it.
- **Your Mac password**, every time it starts. **Nothing shows while you type it.
  That's normal.** Press Enter after.

### Fishing

1. Open Roblox and **join a Fisch server**. Equip your rod.
2. Start XTernal and wait until the window says **Connection: Attached**. The
   first time, and after every Roblox update, it sets itself up automatically;
   that takes under a minute.
3. **Click into the Roblox window**, then press **F1** (on a laptop:
   **fn + F1**). Press it again to stop.

Keep the Terminal window open while it runs; closing it closes the macro.
The macro only clicks while Roblox is the front window. If you click away
it pauses, and it continues when you click back into Roblox.

| Key | What it does |
|---|---|
| F1 | start / stop fishing |
| F2 | stop appraising |
| F3 | reattach to Roblox |
| F5 | start / stop auto appraise |

### Something wrong?

Click **Make support report** in the macro window. A file called
`xternal-report-<date>.zip` appears on your **Desktop**. **Send that file.**
If the macro crashes, it makes this file by itself.

---

## For maintainers

### Repo location

The installer pulls from `avezz1234/MacMacro` (branch `main`). If the repo
moves, change it in **two places**: the `curl` line above and the `REPO=` line
at the top of [`get.sh`](get.sh). `XTernal.command` must stay within four
folders of the repo root.

### The launch flow

`get.sh` downloads into `~/XTernal` (keeping `.venv`) and writes a two-line
Desktop launcher. Then [`XTernal.command`](XTernal.command):

1. runs `install.sh` if `.venv` is missing, broken, or older than
   `requirements.txt`. The installer needs nothing pre-installed: it downloads
   [uv](https://github.com/astral-sh/uv) into `.uv/`, which fetches a
   standalone CPython 3.12 into `.python/` and installs the requirements into
   `.venv/`. System Pythons are ignored (`UV_PYTHON_PREFERENCE=only-managed`),
   and `get.sh` never deletes those three folders when updating;
2. runs `run.py preflight` as the normal user (`xternal/preflight.py`). It
   checks SIP, then Accessibility, then Input Monitoring. It stops with
   step-by-step instructions for SIP; for the two permissions it opens the
   Settings pane and polls with a fresh process until the switch is on.
   Input Monitoring is optional and skips itself after 2 minutes;
3. runs `./xternal-mac`, which re-executes itself under `sudo`;
4. on a non-zero exit, runs `./xternal-mac report` by itself.

Calibration is automatic (`Controller.watch`). It recalibrates when there are
no offsets, when the installed Roblox version differs from the one stored in
`offsets.mac.json`, or when an attach fails the structure test. A calibration
that finds only the Roblox menu's DataModel (no Fisch PlaceId) is discarded
and retried every 15 s rather than saved. After 3 real failures in a row it
stops retrying and asks for a support report. Pressing Calibrate re-arms it.

CLI equivalents, run from the install folder: `./xternal-mac` (window),
`doctor`, `calibrate [--dynamic]`, `report`, `dump`, `preflight`.

Files: log at `~/Library/Logs/xternal-mac/xternal.log` (also in Console.app);
settings, configs and offsets in `~/Library/Application Support/xternal-mac/`.
To uninstall, delete `~/XTernal`, the Desktop launcher and those two folders,
then turn SIP back on with `csrutil enable` from Recovery.

### How it relates to the other builds

This is the Linux port (`xternal-linux`) with its platform layer replaced.
The fishing logic (`xternal/logic/`: the phase machine, PID reel tracking, the
per-rod controllers, totem, appraise, webhook, catch log) and the offset
calibrator are carried over unchanged apart from import lines and log calls.
Settings and config files use the same keys, so configs move between all
three builds.

| Windows original | Linux port | This port |
|---|---|---|
| `OpenProcess` + `ReadProcessMemory` | `process_vm_readv` | `task_for_pid` + `mach_vm_read_overwrite` |
| `VirtualQueryEx` | `/proc/<pid>/maps` + `smaps` | `mach_vm_region_recurse` (resident pages, share mode, VM tag) |
| `Send` / `Click` | `/dev/uinput` | `CGEventPost` at the HID tap |
| `Hotkey()` hook | evdev | polling `CGEventSourceKeyState` (hardware state, 25 ms) |
| n/a | Hyprland focus socket | `CGWindowListCopyWindowInfo` front-to-back |
| n/a | `hyprctl movecursor` | window bounds + GUI scale/inset → screen points (`inputs/window.py`) |
| `%APPDATA%` | XDG dirs | `~/Library/Application Support`, `~/Library/Logs` |
| animated GUI | Qt + pixel-art scene | plain Qt tabs |

Everything native goes through `ctypes` in `xternal/macos/native.py`, with no
pyobjc and no compiled extensions, so the only dependencies are
`PySide6-Essentials` and `numpy`.

### Offsets on macOS

The Mac client is a Clang/libc++ build, like the Android build the Linux port
reads under Sober, so the bundled Windows `offsets.json` doesn't apply and the
calibrator recovers the layout from the live process the same way. One
addition: libc++ has two little-endian `std::string` layouts (standard, and
`_LIBCPP_ABI_ALTERNATE_STRING_LAYOUT`). The calibrator tries both and records
the one the client uses as `string_abi` in `offsets.mac.json`
(`xternal/memory/sso.py`). It also stores the Roblox version it calibrated
against, so `doctor` can say when a recalibration is due.

### Why root + SIP

`task_for_pid` needs root. Roblox is notarized with the Hardened Runtime and
no `get-task-allow`, so with SIP's debugging restriction on, AMFI denies
`task_for_pid` even to root. `csrutil enable --without debug` lifts only that
restriction. The macro re-executes itself under `sudo` and hands every file it
writes back to `$SUDO_USER`, so logs and configs never end up root-owned.

### Logging

- File: DEBUG and above, rotated 2 MB × 5. Terminal: INFO and above.
- Each session starts with a header: macOS build, CPU, Python, PySide6/numpy,
  root/SIP state, TCC permission state, Roblox version and architectures,
  display, offsets summary.
- Logged events: attach steps and timings, heap-region summary by VM tag, every
  calibration step and the resulting table, phase transitions (DEBUG), rod
  changes, catches, focus changes, settings changes (webhook URL masked),
  click-mapping inputs whenever they change, and uncaught exceptions on any
  thread with tracebacks. Repeating per-tick errors are logged once in full,
  then as counts (`log_once`).
- `report` bundles logs, `doctor` output, system info, redacted settings, the
  offsets file, the on-screen window list, and the last 200 catches.

### Not yet verified on real hardware

This was written and tested on Linux. The platform-independent parts were run
there: the string decoders and the calibrator's string search on a synthetic
heap (both layouts), settings, `doctor`/`report`, and the full window
offscreen. The Mac-specific layer has **not** been run against a live Roblox
yet. In rough order of risk:

1. Calibration against the real Mac client: whether the heap filter in
   `memory/proc.py` (`scannable_regions`) includes the arena the DataModel
   lives in. If calibration can't find `DataModel`, the region summary by VM
   tag in the log is the first thing to check.
2. Whether Roblox accepts `CGEventPost` input for the cast (held left button)
   and the shake (`\` + Enter).
3. Stellarwave click placement (`inputs/window.py`). It depends on the GUI
   inset/scale estimate, and the log prints every input to that calculation.
4. Hotkey polling without Input Monitoring on newer macOS versions.
