"""
XTernal for Mac -- the UI-independent half of the app.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Owns everything the window drives but does not draw: the game link, the input
sender, the engine, global hotkeys, the focus guard, attaching and
calibration. The window calls pump() on a timer and listens through `notify`.

Threading is the Linux port's: the engine is single-threaded and runs on the
UI thread; hotkeys, focus changes and attach/calibrate workers live on their
own threads and hand results back through a queue.
"""

from __future__ import annotations

import queue
import threading
import time

from .. import settings as cfg
from ..inputs.focus import FocusGatedSender, FocusGuard
from ..inputs.hotkeys import HotkeyListener
from ..inputs.sender import InputError, InputSender, NullSender
from ..log import FILE_ONLY, get_logger, log_once
from ..logic.engine import MacroEngine
from ..logic.game import GameLink, now_ms
from ..logic.weather import WeatherState, read_weather
from ..logic.webhook import WebhookReporter
from ..macos import native

log = get_logger()

#: How long to wait before re-attempting an attach that failed for a reason
#: retrying cannot fix (stale offsets, no memory access).
ATTACH_RETRY_HOLD_S = 30.0

#: Automatic calibration: how long to wait before trying again when the player
#: isn't in Fisch yet, and how many real failures in a row before giving up
#: and asking for a support report instead of looping.
AUTO_CALIB_WAIT_S = 15.0
AUTO_CALIB_MAX_FAILS = 3

WORLD_POLL_MS = 2000

NOTABLE_WEATHER = {
    "aurora", "rainbow", "eclipse", "blood_moon", "starfall", "frost_moon",
    "tropical_sun", "sovereign", "celestial", "abyssal", "meteor",
    "raging_squall", "tropical_squall",
}


class Controller:
    def __init__(self, screen_size=(0, 0)):
        self.settings = cfg.load_settings()
        self.link = GameLink()
        self.webhook = WebhookReporter(self.settings)
        self.events: queue.Queue = queue.Queue()
        self.notify = lambda kind, payload=None: None

        guard_on = bool(self.settings.get("input", {}).get("focus_guard", 1))
        self.focus = FocusGuard(
            pid_getter=lambda: self.link.client.pid if self.link.client else 0,
            on_change=lambda f: self.events.put(("focus", f)),
            enabled=guard_on,
        )
        self.sender, self.input_error = self._make_sender()
        self.backend_name = "CGEvent" if not isinstance(self.sender, NullSender) else "none"

        self.engine = MacroEngine(self.link, self.sender, self.settings, self.webhook)
        self.engine.screen_size = screen_size
        _engine_on_catch = self.engine.on_catch_logged

        def _catch_hook(catch):
            log.info("catch: %s %.1fkg %s", catch.display, catch.weight or 0, catch.rarity or "")
            _engine_on_catch(catch)
            self.notify("catch", catch)

        self.engine.on_catch_logged = _catch_hook

        self.hotkeys = HotkeyListener()
        self.attach_busy = False
        self.calibrating = False
        self.attach_hold_until = 0.0
        self.detail = ""
        self.weather = WeatherState()
        self._world_at = 0
        self._last_rod = None
        self._last_status = None
        self.auto_calib_fails = 0
        self._roblox_version: tuple[int, str] = (0, "")
        log.info("controller ready: input=%s focus_guard=%s screen=%s",
                 self.backend_name, self.focus.supported, screen_size)

    # -- input ------------------------------------------------------------

    def _make_sender(self):
        try:
            sender = InputSender()
        except InputError as exc:
            log.error("input unavailable: %s", exc)
            return NullSender(), str(exc)
        problem = ""
        ax = native.accessibility_trusted()
        post = native.post_event_access()
        log.info("permissions: accessibility=%s post_events=%s input_monitoring=%s",
                 ax, post, native.listen_event_access())
        if ax is False and post is False:
            problem = ("Terminal doesn't have Accessibility permission, so clicks and "
                       "keys won't reach Roblox. System Settings > Privacy & Security > "
                       "Accessibility > turn on Terminal, then restart the macro.")
            log.warning(problem)
        if self.focus.supported:
            self.focus.start()
            return FocusGatedSender(sender, self.focus), problem
        return sender, problem

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        handlers = {
            "start_macro": lambda _n: self.events.put(("hk_start", "")),
            "stop_appraise": lambda _n: self.events.put(("hk_stop_appraise", "")),
            "fix_roblox": lambda _n: self.events.put(("hk_reattach", "")),
            "reload": lambda _n: self.events.put(("hk_reload", "")),
            "toggle_appraise": lambda _n: self.events.put(("hk_toggle_appraise", "")),
        }
        self.hotkeys.register_all(self.settings["hotkeys"], handlers)
        if not self.hotkeys.start():
            log.warning("hotkeys unavailable: %s", self.hotkeys.error)
        if self.hotkeys.error:
            self.set_detail(self.hotkeys.error)
        if self.input_error:
            self.set_detail(self.input_error)
            self.notify("toast", ("bad", self.input_error))

    def shutdown(self) -> None:
        log.info("shutdown (events sent this session: %s)", getattr(self.sender, "sent", "?"))
        try:
            self.engine.release_all_fishing_mouse(force=True)
        except Exception:
            log.exception("release on shutdown failed")
        self.hotkeys.stop()
        self.focus.stop()
        try:
            self.sender.close()
        except Exception:
            log.exception("sender close failed")

    def set_detail(self, text: str) -> None:
        self.detail = text
        self.notify("detail", text)

    # -- the loop ---------------------------------------------------------

    def pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass

        if self.link.is_memory_ready():
            try:
                self.engine.refresh_rod()
                if self.engine.rod != self._last_rod:
                    log.info("rod: %r -> %r", self._last_rod, self.engine.rod)
                    self._last_rod = self.engine.rod
                self.engine.tick()
            except Exception as exc:
                self.set_detail(f"Tick error: {exc}")
                log_once(log, f"tick:{type(exc).__name__}:{exc}",
                         "tick error in phase %s: %s", self.engine.state.phase, exc,
                         exc_info=True)
            if now_ms() - self._world_at >= WORLD_POLL_MS:
                self._world_at = now_ms()
                try:
                    if self.link.is_in_fisch_game():
                        self._update_weather(read_weather(self.engine.game))
                except Exception as exc:
                    log_once(log, f"weather:{exc}", "weather read failed: %s", exc,
                             exc_info=True, level=20)

        status = self.link.status_text()
        if status != self._last_status:
            log.info("status: %s", status)
            self._last_status = status

    def _update_weather(self, new: WeatherState) -> None:
        old = self.weather
        self.weather = new
        if new.label() == old.label():
            return
        log.info("weather: %s -> %s", old.label(), new.label())
        started = (new.tags - old.tags) & NOTABLE_WEATHER
        if started and (old.cycle or old.base):
            fresh = [n for n in new.names if n not in old.names]
            self.notify("toast", ("good", f"Weather: {', '.join(fresh) or new.label()} started."))

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "detail":
            self.set_detail(payload)
        elif kind == "attached":
            log.info("attached: %s", self.link.client)
            self.set_detail("Attached. Press F1 (or Start) while in Roblox.")
            self.engine.refresh_rod(force=True)
            self.notify("attached")
        elif kind == "attach_failed":
            self.notify("toast", ("warn", payload))
        elif kind == "attach_done":
            self.attach_busy = False
            if not self.link.is_memory_ready() and self.link.attach_fail_reason in ("offsets", "access"):
                self.attach_hold_until = time.monotonic() + ATTACH_RETRY_HOLD_S
        elif kind == "calibrated":
            self.auto_calib_fails = 0
            self.link.attach_fail_reason = ""
            self.set_detail(f"Setup complete ({payload}). Attaching...")
            self.notify("toast", ("good", "Setup complete."))
            self._attach_async()
        elif kind == "calib_not_in_game":
            self.attach_hold_until = time.monotonic() + AUTO_CALIB_WAIT_S
            self.set_detail(WAITING_FOR_FISCH)
            if not payload:
                self.notify("toast", ("warn", "Join a Fisch server first (not the Roblox menu)."))
        elif kind == "calib_failed":
            self.auto_calib_fails += 1
            self.attach_hold_until = time.monotonic() + ATTACH_RETRY_HOLD_S
            log.warning("calibration failure %d/%d", self.auto_calib_fails, AUTO_CALIB_MAX_FAILS)
            self.set_detail(f"Setup failed: {payload}")
            self.notify("toast", ("bad", f"Setup failed: {payload}"))
        elif kind == "calib_done":
            self.calibrating = False
            self.notify("calibrating", False)
        elif kind == "focus":
            log.info("roblox frontmost: %s (front app %r)", payload, self.focus.active_app)
            if not payload:
                if isinstance(self.sender, FocusGatedSender):
                    self.sender.on_focus_lost()
                self.engine.state.is_holding = False
                self.engine.state.is_holding_right = False
            self.notify("focus", payload)
        elif kind == "hk_start":
            log.info("hotkey: start/stop")
            self.toggle_macro()
        elif kind == "hk_stop_appraise":
            if self.engine.state.phase == "APPRAISE" and self.engine.state.cycle_enabled:
                self.engine.stop_appraise_cycle("OFF", "Stopped by hotkey.")
                self.notify("running", False)
        elif kind == "hk_toggle_appraise":
            self.toggle_auto_appraise()
        elif kind == "hk_reattach":
            log.info("hotkey: reattach")
            self.reattach()
        elif kind == "hk_reload":
            self.set_detail("Reload isn't used on Mac; use Reattach (F3).")

    def watch(self) -> None:
        """Every couple of seconds: notice the client, attach when it is ready."""
        client = self.link.poll_client()
        if self.attach_busy or self.calibrating or self.link.is_memory_ready():
            return
        if time.monotonic() < self.attach_hold_until:
            return
        if self.engine.state.phase != "OFF" or client is None:
            return
        why = self._calibration_needed(client)
        if not why:
            self._attach_async()
        elif self.auto_calib_fails >= AUTO_CALIB_MAX_FAILS:
            if self.detail != CALIB_GAVE_UP:
                self.set_detail(CALIB_GAVE_UP)
        else:
            log.info("auto-calibrate: %s", why)
            self.calibrate(auto=True)

    def _current_roblox_version(self, client) -> str:
        """Installed Roblox version, looked up once per Roblox process."""
        if self._roblox_version[0] != client.pid:
            from ..diagnostics import roblox_bundle_info

            self._roblox_version = (client.pid, roblox_bundle_info(client.detail)["version"])
        return self._roblox_version[1]

    def _calibration_needed(self, client) -> str:
        """Why the offsets must be (re)built before attaching, or '' if they're fine."""
        path = cfg.offsets_path()
        if not path.exists():
            return "no offsets yet"
        if self.link.attach_fail_reason == "offsets":
            return "offsets did not match the running game"
        try:
            import json

            saved = json.loads(path.read_text(encoding="utf-8")).get("version", "")
        except (OSError, ValueError):
            return "offsets file unreadable"
        current = self._current_roblox_version(client)
        if saved and current and saved != current:
            return f"Roblox updated ({saved} -> {current})"
        return ""

    # -- actions ----------------------------------------------------------

    def running(self) -> bool:
        return bool(self.engine.state.cycle_enabled)

    def toggle_auto_appraise(self) -> None:
        engine = self.engine
        s = engine.state
        if s.phase == "APPRAISE" and s.cycle_enabled:
            engine.stop_appraise_cycle("OFF", "Stopped by hotkey.")
            self._set_auto_appraise(False)
            log.info("hotkey: auto appraise stopped")
            self.notify("toast", ("warn", "Auto appraise stopped."))
            self.notify("running", False)
            return
        if s.cycle_enabled:
            engine.start()  # stops the fishing cycle
        self._set_auto_appraise(True)
        ok, message = engine.start()
        running = s.phase == "APPRAISE" and s.cycle_enabled
        log.info("auto appraise start ok=%s running=%s msg=%s", ok, running, message)
        if running:
            self.notify("toast", ("good", "Auto appraising..."))
        else:
            if not ok:
                self._set_auto_appraise(False)
            self.notify("toast", ("good" if ok else "warn", message or "Not started."))
        self.notify("running", None)

    def _set_auto_appraise(self, on: bool) -> None:
        self.commit("main.auto_appraise_enabled", on)
        self.notify("setting", ("main.auto_appraise_enabled", on))

    def toggle_macro(self) -> None:
        ok, message = self.engine.start()
        log.info("start/stop: ok=%s running=%s rod=%r msg=%s",
                 ok, self.running(), self.engine.rod, message)
        if not ok:
            self.notify("toast", ("warn", message))
        elif self.running() and not self.focus.game_focused:
            self.notify("toast", ("warn", "Click into Roblox -- input is paused until it's in front."))
        self.set_detail(message)
        self.notify("running", self.running())

    def reattach(self) -> None:
        if self.attach_busy:
            return
        self.engine.game.clear_phase_cache()
        self._attach_async(manual=True)

    def _attach_async(self, manual: bool = False) -> None:
        self.attach_busy = True
        self.attach_hold_until = 0.0
        self.set_detail("Attaching...")

        def worker():
            try:
                self.link.attach(progress=lambda s: self.events.put(("detail", s)))
                self.events.put(("attached", ""))
            except Exception as exc:
                log.info("attach failed: %s", exc)
                if not str(exc):
                    log.exception("attach crashed")
                self.events.put(("attach_failed", str(exc)) if manual else ("detail", str(exc)))
            finally:
                self.events.put(("attach_done", ""))

        threading.Thread(target=worker, daemon=True, name="attach").start()

    def can_calibrate(self) -> tuple[bool, str]:
        client = self.link.poll_client()
        if client is None:
            return False, "Roblox is not running."
        if self.calibrating:
            return False, "Calibration is already running."
        return True, ""

    def calibrate(self, auto: bool = False) -> None:
        ok, why = self.can_calibrate()
        if not ok:
            if not auto:
                self.notify("toast", ("warn", why))
            return
        client = self.link.poll_client()
        self.calibrating = True
        self.notify("calibrating", True)
        if not auto:
            self.auto_calib_fails = 0  # a manual press re-arms automatic retries
        log.info("calibration started (%s): %s", "auto" if auto else "manual", client)
        if auto:
            self.set_detail("Setting up for this Roblox version (only happens once per update)...")

        def worker():
            from ..diagnostics import roblox_bundle_info
            from ..memory.calibrate import calibrate as run_calibration
            from ..memory.offsets import save_calibrated_offsets
            from ..memory.proc import MemoryAccessError, ProcessMemory

            def progress(msg):
                log.info("calibrate: %s", msg)
                self.events.put(("detail", msg))

            try:
                mem = ProcessMemory(client.pid)
                log.info("calibrate: %s", mem.region_summary())
                result = run_calibration(mem, place_id=cfg.FISCH_PLACE_ID, progress=progress)
                for step in result.steps:
                    log.info("calibrate step: %s", step)
                if result.ok and not result.offsets.has("PlaceId"):
                    # Found a DataModel, but it was the Roblox menu's, not
                    # Fisch's. A table built from that can't read the game.
                    log.info("calibrate: no Fisch PlaceId -- player not in Fisch yet")
                    self.events.put(("calib_not_in_game", auto))
                elif result.ok:
                    result.offsets.build_version = roblox_bundle_info(client.detail)["version"]
                    save_calibrated_offsets(result.offsets, cfg.offsets_path())
                    log.info("calibrate: saved\n%s", result.offsets.to_json(), extra=FILE_ONLY)
                    self.events.put(("calibrated", f"{len(result.offsets.table)} offsets"))
                else:
                    log.error("calibrate failed: %s", result.error)
                    self.events.put(("calib_failed", result.error))
            except MemoryAccessError as exc:
                log.error("calibrate: %s", exc)
                self.events.put(("calib_failed", str(exc)))
            except Exception as exc:
                log.exception("calibrate crashed")
                self.events.put(("calib_failed", repr(exc)))
            finally:
                self.events.put(("calib_done", ""))

        threading.Thread(target=worker, daemon=True, name="calibrate").start()

    # -- settings ---------------------------------------------------------

    def get(self, key: str):
        section, name = key.split(".", 1)
        return self.settings[section][name]

    def commit(self, key: str, raw):
        """Store a control's value, keeping the stored value's type.

        Returns the value actually stored, or None if `raw` did not parse.
        """
        section, name = key.split(".", 1)
        current = self.settings[section][name]
        try:
            if isinstance(raw, bool):
                value = int(raw) if isinstance(current, int) and not isinstance(current, bool) else raw
            elif isinstance(current, bool):
                value = bool(raw)
            elif isinstance(current, int):
                value = int(float(str(raw).strip()))
            elif isinstance(current, float):
                value = float(str(raw).strip())
            else:
                value = str(raw)
        except ValueError:
            return None
        if value != current:
            log.info("setting %s: %r -> %r", key,
                     "<url>" if "url" in key and current else current,
                     "<url>" if "url" in key and value else value)
        self.settings[section][name] = value
        cfg.write_settings(self.settings)
        return value

    def load_config(self, name: str) -> bool:
        try:
            self.settings["main"].update(cfg.load_config(name))
        except (OSError, ValueError) as exc:
            self.notify("toast", ("bad", f"Could not load {name}: {exc}"))
            return False
        self.settings["last_config"] = name
        cfg.write_settings(self.settings)
        log.info("config loaded: %s", name)
        self.notify("toast", ("good", f"Loaded {name}."))
        return True

    def save_config(self, name: str) -> bool:
        try:
            cfg.save_config(name, self.settings["main"])
        except (OSError, ValueError) as exc:
            self.notify("toast", ("bad", str(exc)))
            return False
        self.settings["last_config"] = name
        cfg.write_settings(self.settings)
        log.info("config saved: %s", name)
        self.notify("toast", ("good", f"Saved {name}."))
        return True

    def delete_config(self, name: str) -> None:
        if cfg.delete_config(name):
            log.info("config deleted: %s", name)
            self.notify("toast", ("good", f"Deleted {name}."))

    def test_webhook(self) -> None:
        if not self.webhook.enabled():
            self.notify("toast", ("warn", "Enable the webhook and set a URL first."))
            return
        self.webhook.send_alert("XTernal for Mac", "Test message -- the webhook works.")
        self.notify("toast", ("good", "Test message sent."))


WAITING_FOR_FISCH = "Join a Fisch server -- the macro sets itself up automatically."
CALIB_GAVE_UP = ("Automatic setup keeps failing. Click 'Make support report' and send "
                 "the zip on your Desktop.")
