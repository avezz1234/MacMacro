"""
XTernal for Mac -- Discord webhook reporting.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Port of shared/Webhook.ahk. The Windows build posted through the
WinHttp.WinHttpRequest.5.1 COM object; here it is urllib, on a worker thread so
a slow or unreachable Discord never stalls a fishing tick.

Discord's Components V2 payload shape (flags 32768, type 17 container) is kept
exactly as the original built it, so existing webhooks render identically.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from .game import now_ms

USER_AGENT = "XTernal-Mac (https://discord.gg/openmacro)"


def format_runtime(ms: int) -> str:
    ms = max(0, ms)
    total = ms // 1000
    hours, minutes, seconds = total // 3600, (total % 3600) // 60, total % 60
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_coins(value: int) -> str:
    return f"{round(value):,}"


class WebhookReporter:
    def __init__(self, settings: dict):
        self.settings = settings
        self.started_at = 0
        self.last_summary_at = 0

    @property
    def cfg(self) -> dict:
        return self.settings["webhook"]

    def accent_color(self) -> int:
        try:
            return int(self.settings["appearance"]["accent_color"], 16)
        except (KeyError, ValueError):
            return 0x5AA9FF

    def enabled(self) -> bool:
        return bool(self.cfg["webhook_enabled"] and self.cfg["webhook_url"].strip())

    def begin_session(self) -> None:
        if self.started_at == 0:
            self.started_at = now_ms()
            self.last_summary_at = now_ms()

    # -- transport --------------------------------------------------------

    def _post(self, payload: dict) -> None:
        url = self.cfg["webhook_url"].strip()
        if not url:
            return
        body = json.dumps(payload).encode("utf-8")

        def worker():
            request = urllib.request.Request(
                url + "?with_components=true",
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=10).close()
            except (urllib.error.URLError, OSError, ValueError):
                pass  # reporting is best-effort and must never break fishing

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _container(content_blocks: list, color: int) -> dict:
        return {
            "flags": 32768,
            "components": [
                {"type": 17, "accent_color": color, "components": content_blocks}
            ],
        }

    # -- messages ---------------------------------------------------------

    def send_alert(self, title: str, description: str = "", color: int | None = None) -> None:
        if not self.enabled():
            return
        content = f"## {title}"
        if description:
            content += f"\n{description}"
        blocks = [{"type": 10, "content": content}]
        self._post(self._container(blocks, color if color is not None else self.accent_color()))

    def maybe_send_summary(self, engine) -> None:
        if not self.enabled() or self.started_at == 0:
            return
        interval_ms = max(1, int(self.cfg["webhook_summary_interval_min"])) * 60 * 1000
        if self.last_summary_at and (now_ms() - self.last_summary_at) < interval_ms:
            return
        self._post(self.build_summary(engine))
        self.last_summary_at = now_ms()

    def build_summary(self, engine) -> dict:
        cfg = self.cfg
        state = engine.state
        runtime = now_ms() - self.started_at if self.started_at else 0

        header = "## XTernal Summary"
        if cfg["webhook_summary_session_time"]:
            header += f"\n**Session runtime:** {format_runtime(runtime)}"

        stats = []
        if cfg["webhook_summary_fish"]:
            stats.append(f"**Caught:** {state.fish_caught_count}")
            stats.append(f"**Lost:** {state.fish_lost_count}")
        if cfg["webhook_summary_success_rate"]:
            total = state.fish_caught_count + state.fish_lost_count
            rate = (state.fish_caught_count / total * 100.0) if total else 0.0
            stats.append(f"**Success Rate:** {rate:.1f}%")
        if cfg["webhook_summary_cast_timeouts"]:
            stats.append(f"**Cast Timeouts:** {state.cast_timeout_count}")
        if cfg["webhook_summary_totem_pops"]:
            stats.append(f"**Totems Popped:** {state.totem_pop_count}")

        identity = []
        if cfg["webhook_summary_rod"]:
            identity.append(f"**Rod:** {engine.rod or '---'}")
        if cfg["webhook_summary_config"]:
            identity.append(f"**Config:** {self.settings.get('last_config') or '---'}")
        if cfg["webhook_summary_totem_state"]:
            identity.append(f"**Auto Totem:** {self._totem_state_text(engine)}")

        blocks = [{"type": 10, "content": header}]
        if stats:
            blocks.append({"type": 14})  # separator
            blocks.append({"type": 10, "content": "\n".join(stats)})
        if identity:
            blocks.append({"type": 14})
            blocks.append({"type": 10, "content": "\n".join(identity)})
        return self._container(blocks, self.accent_color())

    def _totem_state_text(self, engine) -> str:
        main = engine.main
        if not main["auto_totem_enabled"]:
            return "Disabled"
        if main["auto_totem_mode"] == "interval":
            return f"Enabled (interval {main['auto_totem_interval_sec']}s)"
        return "Enabled (on expire)"

    def send_appraise_result(self, engine, success: bool, message: str) -> None:
        if not self.enabled():
            return
        state = engine.state
        desired = str(engine.main.get("auto_appraise_mutation", "")).strip() or "---"
        lines = [f"**Desired Mutation:** {desired}", f"**Result:** {message}"]
        if state.appraise_start_coins is not None and state.appraise_end_coins is not None:
            spent = max(0, state.appraise_start_coins - state.appraise_end_coins)
            lines.append(f"**C$ Spent:** {format_coins(spent)} C$")
        if success:
            bonuses = self._bonus_attributes(engine)
            if bonuses:
                lines.append(f"**Bonus Attributes:** {', '.join(bonuses)}")
        title = "Appraisal Finished" if success else "Appraisal Failed"
        self.send_alert(title, "\n".join(lines))

    @staticmethod
    def _bonus_attributes(engine) -> list[str]:
        try:
            subvalues = engine.game.resolve_fish_info_subvalues()
            if not subvalues:
                return []
            text = engine.game.collect_subvalues_text(subvalues).lower()
        except Exception:
            return []
        return [name for key, name in (("shiny", "Shiny"), ("sparkling", "Sparkling"))
                if key in text]
