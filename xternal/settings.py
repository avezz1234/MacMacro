r"""
XTernal for Mac -- settings, configs and themes.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of shared/Settings.ahk and the settings half of shared/Constants.ahk.

Changes from the Windows original, all forced by the platform:

  %APPDATA%\OpenMacro\XTernal   ->  ~/Library/Application Support/xternal-mac
  A_ScriptDir\settings          ->  the same config dir
  (log file)                    ->  ~/Library/Logs/xternal-mac/xternal.log

The macro runs as root (reading another process's memory requires it on
macOS), so "~" is resolved to the *real* user's home via SUDO_USER, and every
file it writes is handed back to that user -- otherwise the configs and logs
would end up owned by root and the user could not open or delete them.

The auto-updater and telemetry are removed rather than ported. Both talked to
openmacro.net -- the updater to download and swap in a Windows .zip build (which
cannot install anything meaningful here), and telemetry to report offset health
keyed by a Windows build hash this client does not have. Neither has a coherent
Linux meaning, and silently phoning home is not something a port should add.
The corresponding settings keys are kept so a settings.json copied from a
Windows install still loads unchanged.
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

APP_NAME = "xternal-mac"
FULL_VER = "v0.2.55-mac.1"

#: Fisch's permanent place id. Reading this back through the PlaceId offset is
#: the reliable "actually in Fisch" signal -- the DataModel resolves on the menu
#: too. Also doubles as the ground truth that pins PlaceId during calibration.
FISCH_PLACE_ID = 16732694052

MIN_CAST_TIMEOUT_MS = 5000


def real_user() -> tuple[str, int, int, Path]:
    """(name, uid, gid, home) of the person who ran the macro, even under sudo."""
    name = os.environ.get("SUDO_USER") or ""
    if name and name != "root":
        try:
            pw = pwd.getpwnam(name)
            return pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir)
        except KeyError:
            pass
    pw = pwd.getpwuid(os.getuid())
    return pw.pw_name, pw.pw_uid, pw.pw_gid, Path.home()


def real_home() -> Path:
    return real_user()[3]


def config_dir() -> Path:
    return real_home() / "Library" / "Application Support" / APP_NAME


def configs_dir() -> Path:
    return config_dir() / "configs"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def offsets_path() -> Path:
    """Where the calibrated offset table is cached between runs."""
    return config_dir() / "offsets.mac.json"


def state_dir() -> Path:
    """Generated data (the catch feed), as opposed to settings the user edits."""
    return config_dir() / "state"


def log_dir() -> Path:
    """~/Library/Logs/xternal-mac -- also browsable in Console.app."""
    return real_home() / "Library" / "Logs" / APP_NAME


def log_path() -> Path:
    return log_dir() / "xternal.log"


def catch_feed_path() -> Path:
    """The JSONL feed of landed fish that external tools follow."""
    return state_dir() / "catches.jsonl"


def give_back(path) -> None:
    """chown a file/dir we created to the real user when running under sudo."""
    if os.geteuid() != 0:
        return
    _name, uid, gid, _home = real_user()
    if uid == 0:
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def fix_ownership() -> None:
    """Hand every file under the macro's own folders back to the real user."""
    if os.geteuid() != 0:
        return
    for root in (config_dir(), log_dir()):
        if not root.exists():
            continue
        give_back(root)
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                give_back(os.path.join(dirpath, name))


def ensure_dirs() -> None:
    for d in (config_dir(), configs_dir(), state_dir(), log_dir()):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            give_back(d)


def default_settings() -> dict:
    return {
        "appearance": {
            "accent_color": "5aa9ff",
            "bg_color": "0f1115",
            "border_color": "2a2f3a",
            "text_color": "f5f7fa",
        },
        "env": "prod",
        "hotkeys": {
            "start_macro": "F1",
            "stop_appraise": "F2",
            "fix_roblox": "F3",
            "reload": "F4",
            "toggle_appraise": "F5",
        },
        # Rod tuning only. Config files are dumps of this section, so anything
        # personal or machine-specific belongs in `webhook` / `user` instead.
        "main": {
            "close_threshold": 0.01,
            "derivative_gain": 0.55,
            "edge_boundary": 0.1,
            "neutral_duty_cycle": 0.5,
            "prediction_strength": 7.5,
            "proportional_gain": 0.42,
            "resilience": 0.0,
            "update_rate": 21,
            "velocity_damping": 38,
            "cast_mode": "short",
            "cast_power_custom": 96.0,
            "cast_timeout_ms": 15000,
            "pre_cast_delay_ms": 0,
            "post_cast_delay_ms": 150,
            "cast_on_timeout": 1,
            "fishing_action_delay_ms": 0,
            "completion_threshold": 99.7,
            "shake_interval_ms": 25,
            "stellarwave_auto_signs": 1,
            "early_cast": 1,
            # Assisted catch: the player fishes, the macro only plays the
            # stages switched on here. Off = full auto, as before.
            "assist_enabled": 0,
            "assist_cast": 0,
            "assist_shake": 1,
            "assist_reel": 1,
            "assist_minigames": 1,
            "stellarwave_start_delay_ms": 0,
            "stellarwave_click_gap_ms": 0,
            "auto_appraise_mutation": "Mythical",
            "appraise_delay_ms": 100,
            "auto_totem_enabled": 0,
            "public_server_enabled": 0,
            "auto_totem_name": "Aurora Totem",
            "auto_totem_mode": "expire",
            "auto_totem_interval_sec": 900,
        },
        "webhook": {
            "webhook_url": "",
            "webhook_enabled": 0,
            "webhook_summary_interval_min": 30,
            "webhook_summary_fish": 1,
            "webhook_summary_success_rate": 1,
            "webhook_summary_rod": 1,
            "webhook_summary_config": 1,
            "webhook_summary_totem_state": 1,
            "webhook_summary_totem_pops": 1,
            "webhook_summary_session_time": 1,
            "webhook_summary_cast_timeouts": 1,
            "webhook_alert_totem_failed": 1,
        },
        # Clicks and keys go wherever macOS focus is, so by default the macro
        # only sends them while Roblox is the frontmost app.
        "input": {
            "focus_guard": 1,
        },
        "user": {
            "lullaby_mode": "Prismatic",
            "auto_appraise_click_x": "",
            "auto_appraise_click_y": "",
            "stellarwave_offset_x": 0,
            "stellarwave_offset_y": 0,
        },
        "last_config": "",
        "last_theme": "Default",
        "custom_theme": {
            "accent_color": "5aa9ff",
            "bg_color": "0f1115",
            "text_color": "f5f7fa",
            "border_color": "2a2f3a",
        },
    }


#: Keys that used to live in `main` and were moved out, or dropped entirely.
#: Pruned on load so a shared config never carries someone else's webhook URL.
OBSOLETE_MAIN_KEYS = [
    "fishing_end_grace_ms",
    "post_catch_delay_ms",
    "post_totem_delay_ms",
    "auto_appraise_max_cash",
    "auto_appraise_click_delay_ms",
    "auto_appraise_check_delay_ms",
    "auto_appraise_retry_delay_ms",
    "auto_appraise_enabled",
    "webhook_url",
    "webhook_enabled",
    "webhook_summary_interval_min",
    "webhook_summary_fish",
    "webhook_summary_success_rate",
    "webhook_summary_rod",
    "webhook_summary_config",
    "webhook_summary_totem_state",
    "webhook_summary_totem_pops",
    "webhook_summary_session_time",
    "webhook_summary_cast_timeouts",
    "webhook_alert_totem_failed",
    "lullaby_mode",
    "auto_appraise_click_x",
    "auto_appraise_click_y",
]


def builtin_themes() -> dict:
    return {
        "Default": {"accent_color": "5aa9ff", "bg_color": "0f1115",
                    "text_color": "f5f7fa", "border_color": "2a2f3a"},
        "Crimson": {"accent_color": "ff4c4c", "bg_color": "1a0a0a",
                    "text_color": "f5e6e6", "border_color": "3a1f1f"},
        "Emerald": {"accent_color": "3ddfa0", "bg_color": "0a1512",
                    "text_color": "e6f5ef", "border_color": "1f3a2d"},
        "Amber":   {"accent_color": "ffb347", "bg_color": "15120a",
                    "text_color": "f5f0e6", "border_color": "3a331f"},
        "Lavender": {"accent_color": "b388ff", "bg_color": "120e18",
                     "text_color": "ede6f5", "border_color": "2d1f3a"},
        "Arctic":  {"accent_color": "88cfff", "bg_color": "e8edf2",
                    "text_color": "1a1e24", "border_color": "c0c8d4"},
        "Slate":   {"accent_color": "78909c", "bg_color": "1e272e",
                    "text_color": "cfd8dc", "border_color": "37474f"},
    }


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value).strip())
        return True
    except (TypeError, ValueError):
        return False


def _normalize_main(main: dict) -> bool:
    """NormalizeMainSettings(): clamp the values a bad edit could break."""
    changed = False
    if _is_number(main.get("cast_timeout_ms")):
        normalized = max(MIN_CAST_TIMEOUT_MS, round(float(main["cast_timeout_ms"])))
        if normalized != main["cast_timeout_ms"]:
            main["cast_timeout_ms"] = normalized
            changed = True
    if "auto_appraise_mutation" in main:
        normalized = str(main["auto_appraise_mutation"]).strip() or "Mythical"
        if normalized != main["auto_appraise_mutation"]:
            main["auto_appraise_mutation"] = normalized
            changed = True
    return changed


def _normalize_user(user: dict) -> bool:
    changed = False
    for key in ("auto_appraise_click_x", "auto_appraise_click_y"):
        if key not in user:
            continue
        value = str(user[key]).strip()
        normalized = round(float(value)) if value and _is_number(value) else ""
        if normalized != user[key]:
            user[key] = normalized
            changed = True
    return changed


def load_settings() -> dict:
    """LoadSettings(): read, migrate, backfill and persist in one pass."""
    ensure_dirs()
    path = settings_path()
    defaults = default_settings()

    if not path.exists():
        write_settings(defaults)
        _install_runtime_defaults(defaults)
        return defaults

    try:
        with open(path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Failed to load settings: {exc}") from exc

    changed = False

    for section in ("appearance", "hotkeys", "main", "webhook", "user",
                    "custom_theme", "input"):
        if not isinstance(settings.get(section), dict):
            settings[section] = dict(defaults[section])
            changed = True

    # The webhook/user split: values used to live in `main`, so migrate any
    # still sitting there before the prune below removes them.
    for section in ("webhook", "user", "input"):
        for key, default_value in defaults[section].items():
            if key not in settings[section]:
                settings[section][key] = settings["main"].get(key, default_value)
                changed = True

    for key, value in defaults["hotkeys"].items():
        if key not in settings["hotkeys"]:
            settings["hotkeys"][key] = value
            changed = True

    for key, value in defaults["main"].items():
        if key not in settings["main"]:
            settings["main"][key] = value
            changed = True

    for key in OBSOLETE_MAIN_KEYS:
        if key in settings["main"]:
            del settings["main"][key]
            changed = True

    for key in ("last_config", "last_theme"):
        if key not in settings:
            settings[key] = defaults[key]
            changed = True

    changed |= _normalize_main(settings["main"])
    changed |= _normalize_user(settings["user"])

    if changed:
        write_settings(settings)

    _install_runtime_defaults(settings)
    return settings


def _install_runtime_defaults(settings: dict) -> None:
    """Session-only keys that live in memory but never on disk.

    Auto-appraise is deliberately off at every launch, exactly as the Windows
    build does it (Constants.ahk assigns MAIN["auto_appraise_enabled"] := 0
    after loading, and the key is in the obsolete list so it is stripped from
    the file). Starting a session in appraise mode by accident burns coins.
    """
    settings["main"].setdefault("auto_appraise_enabled", 0)
    settings["main"]["auto_appraise_enabled"] = 0


def write_settings(settings: dict) -> None:
    ensure_dirs()
    # Never persist the runtime-only keys, so the file stays canonical no matter
    # how many times the UI writes it back.
    payload = dict(settings)
    payload["main"] = {
        k: v for k, v in settings.get("main", {}).items() if k not in OBSOLETE_MAIN_KEYS
    }
    tmp = settings_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4), encoding="utf-8")
    tmp.replace(settings_path())  # atomic: never leave a half-written file
    give_back(settings_path())


# ---------------------------------------------------------------------------
# Named rod configs (dumps of the `main` section)
# ---------------------------------------------------------------------------

CONFIG_SCHEMA_VERSION = 1


def list_configs() -> list[str]:
    ensure_dirs()
    return sorted(p.stem for p in configs_dir().glob("*.json"))


def save_config(name: str, main: dict) -> Path:
    ensure_dirs()
    name = name.strip()
    if not name:
        raise ValueError("Config name cannot be empty.")
    if "/" in name or name.startswith("."):
        raise ValueError("Config name cannot contain '/' or start with '.'")
    payload = {
        "schema": CONFIG_SCHEMA_VERSION,
        "main": {k: v for k, v in main.items() if k not in OBSOLETE_MAIN_KEYS},
    }
    path = configs_dir() / f"{name}.json"
    path.write_text(json.dumps(payload, indent=4), encoding="utf-8")
    give_back(path)
    return path


def load_config(name: str) -> dict:
    path = configs_dir() / f"{name}.json"
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    main = payload.get("main", payload)  # tolerate a bare dump
    return {k: v for k, v in main.items() if k not in OBSOLETE_MAIN_KEYS}


def delete_config(name: str) -> bool:
    path = configs_dir() / f"{name}.json"
    if path.exists():
        path.unlink()
        return True
    return False
