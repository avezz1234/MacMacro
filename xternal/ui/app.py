"""
XTernal for Mac -- the window.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Deliberately plain: stock Qt widgets in tabs, no animated scene, no custom
painting. Every setting the Linux UI exposes is here, plus a live Log tab and
a one-click support report, because whoever runs this is not the person who
debugs it.
"""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import log as xlog
from .. import settings as cfg
from ..settings import FULL_VER
from .controller import Controller

log = xlog.get_logger()

COLORS = {"good": "#2e7d32", "warn": "#b26a00", "bad": "#c62828", "": "#555555"}


class _LogBridge(QObject):
    """Carries log lines from any thread onto the UI thread."""

    line = Signal(int, str)


class Window(QMainWindow):
    def __init__(self, ctl: Controller):
        super().__init__()
        self.ctl = ctl
        self.bound: dict[str, tuple[QWidget, callable]] = {}
        self.setWindowTitle(f"XTernal for Mac  {FULL_VER}")
        self.resize(760, 640)

        tabs = QTabWidget()
        tabs.addTab(self._tab_main(), "Main")
        tabs.addTab(self._scroll(self._tab_cast()), "Cast && Reel")
        tabs.addTab(self._scroll(self._tab_assist()), "Assist")
        tabs.addTab(self._scroll(self._tab_totem()), "Totem")
        tabs.addTab(self._scroll(self._tab_appraise()), "Appraise")
        tabs.addTab(self._scroll(self._tab_webhook()), "Webhook")
        tabs.addTab(self._tab_catches(), "Catches")
        tabs.addTab(self._tab_log(), "Log")
        tabs.addTab(self._scroll(self._tab_help()), "Help")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("Starting...")

        ctl.notify = self.on_notify

        self.pump_timer = QTimer(self)
        self.pump_timer.timeout.connect(ctl.pump)
        self._apply_update_rate()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(200)
        self.watch_timer = QTimer(self)
        self.watch_timer.timeout.connect(ctl.watch)
        self.watch_timer.start(2000)
        QTimer.singleShot(300, ctl.watch)

    # -- builders ---------------------------------------------------------

    def _scroll(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    def entry(self, key: str, width: int = 110, placeholder: str = "") -> QLineEdit:
        w = QLineEdit(str(self.ctl.get(key)))
        w.setMaximumWidth(width)
        if placeholder:
            w.setPlaceholderText(placeholder)

        def commit():
            stored = self.ctl.commit(key, w.text())
            if stored is None:
                w.setText(str(self.ctl.get(key)))
                self.toast("warn", f"'{w.text()}' isn't a valid value.")
            elif key == "main.update_rate":
                self._apply_update_rate()

        w.editingFinished.connect(commit)
        self.bound[key] = (w, lambda v: w.setText(str(v)))
        return w

    def check(self, key: str, text: str = "") -> QCheckBox:
        w = QCheckBox(text)
        w.setChecked(bool(self.ctl.get(key)))
        w.toggled.connect(lambda on: self.ctl.commit(key, on))

        def setter(v):
            w.blockSignals(True)
            w.setChecked(bool(v))
            w.blockSignals(False)

        self.bound[key] = (w, setter)
        return w

    def combo(self, key: str, values: list[str]) -> QComboBox:
        w = QComboBox()
        w.setMaximumWidth(220)
        w.addItems(values)
        current = str(self.ctl.get(key))
        if current not in values:
            w.addItem(current)
        w.setCurrentText(current)
        w.currentTextChanged.connect(lambda t: self.ctl.commit(key, t))

        def setter(v):
            w.blockSignals(True)
            w.setCurrentText(str(v))
            w.blockSignals(False)

        self.bound[key] = (w, setter)
        return w

    @staticmethod
    def _hint(widget: QWidget, hint: str) -> QWidget:
        if not hint:
            return widget
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        h = QLabel(hint)
        h.setStyleSheet("color: gray;")
        row.addWidget(h)
        row.addStretch(1)
        return box

    def _group(self, title: str, rows: list, note: str = "") -> QGroupBox:
        g = QGroupBox(title)
        form = QFormLayout(g)
        if note:
            n = QLabel(note)
            n.setWordWrap(True)
            n.setStyleSheet("color: gray;")
            form.addRow(n)
        for row in rows:
            if isinstance(row, QWidget):
                form.addRow(row)
            else:
                label, widget, *hint = row
                form.addRow(label, self._hint(widget, hint[0] if hint else ""))
        return g

    @staticmethod
    def _page(*widgets) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        for w in widgets:
            lay.addWidget(w)
        lay.addStretch(1)
        return page

    # -- tabs -------------------------------------------------------------

    def _tab_main(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)

        status = QGroupBox("Status")
        grid = QGridLayout(status)
        self.lbl_attach = QLabel("...")
        self.lbl_phase = QLabel("OFF")
        self.lbl_rod = QLabel("---")
        self.lbl_weather = QLabel("---")
        self.lbl_focus = QLabel("---")
        for i, (name, w) in enumerate([
            ("Connection", self.lbl_attach), ("Phase", self.lbl_phase),
            ("Rod", self.lbl_rod), ("Weather", self.lbl_weather),
            ("Roblox in front", self.lbl_focus),
        ]):
            grid.addWidget(QLabel(name + ":"), i, 0)
            grid.addWidget(w, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addWidget(status)

        stats = QGroupBox("This session")
        sg = QGridLayout(stats)
        self.stats: dict[str, QLabel] = {}
        for i, (key, caption) in enumerate([
            ("power", "Cast power"), ("progress", "Reel progress"),
            ("caught", "Caught"), ("lost", "Lost"),
            ("rate", "Fish / hour"), ("cycle", "Avg cycle"),
        ]):
            lbl = QLabel("---")
            lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
            self.stats[key] = lbl
            sg.addWidget(QLabel(caption), (i // 3) * 2, i % 3)
            sg.addWidget(lbl, (i // 3) * 2 + 1, i % 3)
        lay.addWidget(stats)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start  (F1)")
        self.start_btn.setMinimumHeight(40)
        f = self.start_btn.font()
        f.setBold(True)
        self.start_btn.setFont(f)
        self.start_btn.clicked.connect(self.ctl.toggle_macro)
        reattach = QPushButton("Reattach  (F3)")
        reattach.clicked.connect(self.ctl.reattach)
        self.calib_btn = QPushButton("Calibrate")
        self.calib_btn.setToolTip("Normally automatic. Press only if setup failed.")
        self.calib_btn.clicked.connect(self._calibrate_clicked)
        for b in (self.start_btn, reattach, self.calib_btn):
            buttons.addWidget(b)
        lay.addLayout(buttons)

        help_row = QHBoxLayout()
        report = QPushButton("Make support report")
        report.setToolTip("Zips the logs and a checkup onto your Desktop -- send that file.")
        report.clicked.connect(self._make_report)
        logs = QPushButton("Open log folder")
        logs.clicked.connect(self._open_logs)
        help_row.addWidget(report)
        help_row.addWidget(logs)
        help_row.addStretch(1)
        lay.addLayout(help_row)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.detail)

        conf = QGroupBox("Config (saved cast + tuning values)")
        row = QHBoxLayout(conf)
        self.config_box = QComboBox()
        self.config_box.setEditable(True)
        self.config_box.addItems(cfg.list_configs())
        self.config_box.setCurrentText(self.ctl.settings.get("last_config", ""))
        self.config_box.lineEdit().setPlaceholderText("config name")
        row.addWidget(self.config_box, 1)
        for text, fn in (("Load", self._load_config), ("Save", self._save_config),
                         ("Delete", self._delete_config)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addWidget(conf)
        lay.addStretch(1)
        return page

    def _tab_cast(self) -> QWidget:
        cast = self._group("Cast", [
            ("Cast power", self.combo("main.cast_mode", ["short", "custom", "full"])),
            ("Custom power", self.entry("main.cast_power_custom"), "% (custom mode)"),
            ("Cast timeout", self.entry("main.cast_timeout_ms"), "ms"),
            ("Pre-cast delay", self.entry("main.pre_cast_delay_ms"), "ms"),
            ("Post-cast delay", self.entry("main.post_cast_delay_ms"), "ms"),
            ("Shake interval", self.entry("main.shake_interval_ms"), "ms"),
            ("", self.check("main.cast_on_timeout", "Re-cast on timeout")),
            ("", self.check("main.early_cast", "Early cast (cast while the last catch finishes)")),
        ])
        reel = self._group("Reel tracking (PID)", [
            ("Proportional gain", self.entry("main.proportional_gain"), "pull toward the fish"),
            ("Derivative gain", self.entry("main.derivative_gain"), "react to fish speed"),
            ("Velocity damping", self.entry("main.velocity_damping"), "brake the bar"),
            ("Prediction strength", self.entry("main.prediction_strength"), "look-ahead"),
            ("Neutral duty cycle", self.entry("main.neutral_duty_cycle"), "hover hold ratio"),
            ("Close threshold", self.entry("main.close_threshold"), "fine-control band"),
            ("Edge boundary", self.entry("main.edge_boundary"), "keep off the walls"),
            ("Completion threshold", self.entry("main.completion_threshold"), "% to call it caught"),
            ("Action delay", self.entry("main.fishing_action_delay_ms"), "ms between clicks"),
            ("Update rate", self.entry("main.update_rate"), "ms per tick"),
        ], "Same keys as the Windows/Linux builds, so shared configs behave the same.")
        rods = self._group("Rod specials", [
            ("Lullaby buff", self.combo("user.lullaby_mode", [
                "Prismatic", "Quickening", "Strenghtening", "Fortuitous", "Resistant"])),
            ("", self.check("main.stellarwave_auto_signs", "Stellarwave: auto-click signs")),
            ("Stellarwave start delay", self.entry("main.stellarwave_start_delay_ms"), "ms"),
            ("Stellarwave click gap", self.entry("main.stellarwave_click_gap_ms"), "ms"),
            ("Click offset X", self.entry("user.stellarwave_offset_x"), "nudge if clicks miss"),
            ("Click offset Y", self.entry("user.stellarwave_offset_y"), "nudge if clicks miss"),
        ])
        return self._page(cast, reel, rods)

    def _tab_assist(self) -> QWidget:
        g = self._group("Assisted catch", [
            ("", self.check("main.assist_enabled", "Enable assisted catch (off = full auto)")),
            ("", self.check("main.assist_cast", "Macro casts")),
            ("", self.check("main.assist_shake", "Macro shakes")),
            ("", self.check("main.assist_reel", "Macro plays the reel bar")),
            ("", self.check("main.assist_minigames", "Macro plays rod minigames")),
        ], "You fish, the macro takes over only the parts ticked here.")
        return self._page(g)

    def _tab_totem(self) -> QWidget:
        g = self._group("Auto totem", [
            ("", self.check("main.auto_totem_enabled", "Enable auto totem")),
            ("Totem", self.combo("main.auto_totem_name", ["Aurora Totem"])),
            ("Mode", self.combo("main.auto_totem_mode", ["expire", "interval"]),
             "expire = once per night"),
            ("Interval", self.entry("main.auto_totem_interval_sec"), "seconds (interval mode)"),
            ("", self.check("main.public_server_enabled",
                            "Public server: skip while Starfall/Rainbow is active")),
        ], "Pops an Aurora Totem (Sundial first if needed). Items are swapped only "
           "between casts, and the rod is re-equipped after.")
        return self._page(g)

    def _tab_appraise(self) -> QWidget:
        g = self._group("Auto appraise", [
            ("", self.check("main.auto_appraise_enabled",
                            "Enable auto appraise (resets every launch)")),
            ("Desired mutation", self.entry("main.auto_appraise_mutation", 200)),
            ("Appraise delay", self.entry("main.appraise_delay_ms"), "ms after a click"),
            ("Click X", self.entry("user.auto_appraise_click_x", 80, "blank"),
             "screen point; blank = click where the cursor is"),
            ("Click Y", self.entry("user.auto_appraise_click_y", 80, "blank")),
        ], "Hold the fish, rest the cursor on the appraise button, then start (F5).")
        self.appraise_status = QLabel("Idle.")
        self.appraise_status.setWordWrap(True)
        status = QGroupBox("Status")
        QVBoxLayout(status).addWidget(self.appraise_status)
        return self._page(g, status)

    def _tab_webhook(self) -> QWidget:
        url = self.entry("webhook.webhook_url", 520, "https://discord.com/api/webhooks/...")
        test = QPushButton("Send test message")
        test.clicked.connect(lambda: (self.ctl.commit("webhook.webhook_url", url.text()),
                                      self.ctl.test_webhook()))
        conn = self._group("Discord webhook", [
            ("", self.check("webhook.webhook_enabled", "Enable webhook")),
            ("Webhook URL", url),
            ("Summary interval", self.entry("webhook.webhook_summary_interval_min"), "minutes"),
            test,
        ], "Nothing else in XTernal touches the network. The URL is never written to logs or reports.")
        parts = self._group("Summary contents", [
            ("", self.check(key, text)) for text, key in [
                ("Fish counts", "webhook.webhook_summary_fish"),
                ("Success rate", "webhook.webhook_summary_success_rate"),
                ("Rod", "webhook.webhook_summary_rod"),
                ("Config", "webhook.webhook_summary_config"),
                ("Totem state", "webhook.webhook_summary_totem_state"),
                ("Totem pops", "webhook.webhook_summary_totem_pops"),
                ("Session time", "webhook.webhook_summary_session_time"),
                ("Cast timeouts", "webhook.webhook_summary_cast_timeouts"),
                ("Alert on totem failure", "webhook.webhook_alert_totem_failed"),
            ]
        ])
        return self._page(conn, parts)

    def _tab_catches(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.catch_summary = QLabel("No catches yet.")
        lay.addWidget(self.catch_summary)
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["Time", "Fish", "Weight", "Rarity"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.catch_table = t
        lay.addWidget(t, 1)
        row = QHBoxLayout()
        copy = QPushButton("Copy summary")
        copy.clicked.connect(self._copy_catches)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_catches)
        row.addWidget(copy)
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        return page

    def _tab_log(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        top = QHBoxLayout()
        self.log_debug = QCheckBox("Show debug lines")
        top.addWidget(self.log_debug)
        top.addStretch(1)
        path = QLabel(f"Full log: {cfg.log_path()}")
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path.setStyleSheet("color: gray;")
        top.addWidget(path)
        lay.addLayout(top)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(self.log_view, 1)

        self._bridge = _LogBridge()
        self._bridge.line.connect(self._append_log)
        self._log_listener = lambda level, line: self._bridge.line.emit(level, line)
        xlog.add_listener(self._log_listener)
        return page

    def _tab_help(self) -> QWidget:
        hk = self.ctl.settings["hotkeys"]
        text = QLabel(
            "<b>Hotkeys</b> (on a laptop press <b>fn</b> + the F key):<br>"
            f"&nbsp;&nbsp;{hk.get('start_macro')} start / stop fishing<br>"
            f"&nbsp;&nbsp;{hk.get('stop_appraise')} stop appraising<br>"
            f"&nbsp;&nbsp;{hk.get('fix_roblox')} reattach to Roblox<br>"
            f"&nbsp;&nbsp;{hk.get('toggle_appraise')} start / stop auto appraise<br><br>"
            "<b>To fish:</b> join a Fisch server and wait for <b>Connection: Attached</b> "
            "(the first time, and after Roblox updates, it sets itself up -- under a "
            "minute). Then click into Roblox and press F1.<br>"
            "<b>Calibrate</b> button: only needed if it says the setup failed.<br>"
            "<b>Something wrong?</b> Main tab &gt; <b>Make support report</b>, send the zip "
            "from your Desktop.<br><br>"
            f"Settings: {cfg.config_dir()}<br>"
            f"Logs: {cfg.log_dir()}<br><br>"
            "<b>About</b><br>XTernal for Mac -- a port of OpenMacro XTernal. Original: "
            "Copyright (c) 2026 (@anorexc), developed by misery (@termx3), maintained by "
            "shinkting (@Invermatic1). https://discord.gg/openmacro<br>"
            "Licensed AGPL-3.0-only; this port is a modified version distributed under the "
            "same licence (see LICENSE and NOTICE)."
        )
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.setTextFormat(Qt.RichText)
        return self._page(text)

    # -- actions ----------------------------------------------------------

    def toast(self, level: str, message: str) -> None:
        self.statusBar().setStyleSheet(f"color: {COLORS.get(level, COLORS[''])};")
        self.statusBar().showMessage(message, 10000)

    def _append_log(self, level: int, line: str) -> None:
        if level < 20 and not self.log_debug.isChecked():
            return
        self.log_view.appendPlainText(line)

    def _apply_update_rate(self) -> None:
        try:
            rate = max(10, int(self.ctl.settings["main"]["update_rate"]))
        except (TypeError, ValueError):
            rate = 21
        self.pump_timer.start(rate)

    def _calibrate_clicked(self) -> None:
        ok, why = self.ctl.can_calibrate()
        if not ok:
            self.toast("warn", why)
            return
        answer = QMessageBox.question(
            self, "Calibrate",
            "Be loaded into a Fisch server (not the Roblox menu).\n\n"
            "This reads the game's memory for a few seconds to work out where this "
            "Roblox version keeps its data. Nothing is written to the game.\n\nStart?",
        )
        if answer == QMessageBox.Yes:
            self.ctl.calibrate()

    def _make_report(self) -> None:
        from ..diagnostics import make_report, reveal_in_finder

        self.toast("", "Making report...")
        QApplication.processEvents()
        try:
            path = make_report()
        except Exception as exc:
            log.exception("report failed")
            self.toast("bad", f"Report failed: {exc}")
            return
        log.info("report written to %s", path)
        reveal_in_finder(path)
        QMessageBox.information(self, "Support report",
                                f"Saved to:\n{path}\n\nSend that zip file to whoever is helping you.")

    def _open_logs(self) -> None:
        from ..diagnostics import reveal_in_finder

        reveal_in_finder(cfg.log_path())

    def _load_config(self) -> None:
        name = self.config_box.currentText().strip()
        if not name or not self.ctl.load_config(name):
            return
        for key, (_w, setter) in self.bound.items():
            section, field = key.split(".", 1)
            if section == "main" and field in self.ctl.settings["main"]:
                setter(self.ctl.settings["main"][field])
        self._apply_update_rate()

    def _save_config(self) -> None:
        name = self.config_box.currentText().strip()
        if not name:
            self.toast("warn", "Type a config name first.")
            return
        if self.ctl.save_config(name):
            self._reload_config_list(name)

    def _delete_config(self) -> None:
        name = self.config_box.currentText().strip()
        if not name:
            return
        if QMessageBox.question(self, "Delete config", f"Delete '{name}'?") == QMessageBox.Yes:
            self.ctl.delete_config(name)
            self._reload_config_list("")

    def _reload_config_list(self, current: str) -> None:
        self.config_box.blockSignals(True)
        self.config_box.clear()
        self.config_box.addItems(cfg.list_configs())
        self.config_box.setCurrentText(current)
        self.config_box.blockSignals(False)

    def _copy_catches(self) -> None:
        catches = self.ctl.engine.catches
        lines = [f"{n}x {name}" for name, n in catches.species_counts()]
        muts = [f"{n}x {name}" for name, n in catches.mutation_counts()]
        text = "Species:\n  " + "\n  ".join(lines or ["none"])
        if muts:
            text += "\n\nMutations:\n  " + "\n  ".join(muts)
        QApplication.clipboard().setText(text)
        self.toast("good", "Summary copied.")

    def _clear_catches(self) -> None:
        self.ctl.engine.catches.entries.clear()
        self.catch_table.setRowCount(0)
        self._refresh_catch_summary()

    # -- notifications ----------------------------------------------------

    def on_notify(self, kind: str, payload=None) -> None:
        if kind == "toast":
            self.toast(*payload)
        elif kind == "catch":
            self._add_catch(payload)
        elif kind == "calibrating":
            self.calib_btn.setEnabled(not payload)
            self.calib_btn.setText("Calibrating..." if payload else "Calibrate")
        elif kind == "setting":
            key, value = payload
            if key in self.bound:
                self.bound[key][1](value)

    def _add_catch(self, catch) -> None:
        t = self.catch_table
        t.insertRow(0)
        values = (
            time.strftime("%H:%M:%S", time.localtime(catch.at)),
            catch.display,
            f"{catch.weight:g} kg" if catch.weight else "---",
            catch.rarity or "---",
        )
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == 3 and catch.rarity_denominator >= 1000:
                item.setForeground(QColor("#c62828"))
            t.setItem(0, col, item)
        while t.rowCount() > 300:
            t.removeRow(t.rowCount() - 1)
        self._refresh_catch_summary()

    def _refresh_catch_summary(self) -> None:
        catches = self.ctl.engine.catches
        rarest = catches.rarest(1)
        self.catch_summary.setText(
            f"Logged: {len(catches.entries)}   Species: {len(catches.species_counts())}   "
            f"Rarest: {f'{rarest[0].display} ({rarest[0].rarity})' if rarest else '---'}"
        )

    # -- refresh ----------------------------------------------------------

    @staticmethod
    def _set(label: QLabel, text: str, color: str = "") -> None:
        if label.text() != text:
            label.setText(text)
        style = f"color: {color};" if color else ""
        if label.property("_style") != style:
            label.setProperty("_style", style)
            label.setStyleSheet(style)

    def refresh(self) -> None:
        ctl, eng = self.ctl, self.ctl.engine
        s = eng.state
        status = ctl.link.status_text()
        running = ctl.running()
        color = COLORS["good"] if status == "Attached" else (
            COLORS["bad"] if "stale" in status or "Can't" in status else COLORS["warn"])
        self._set(self.lbl_attach, status, color)
        phase = eng.status_line or s.phase
        self._set(self.lbl_phase, phase, COLORS["good"] if running else "")
        self._set(self.lbl_rod, eng.rod or "none detected")
        wx = ctl.weather
        self._set(self.lbl_weather, wx.label() if (wx.cycle or wx.base) else "---")
        if not ctl.focus.supported:
            self._set(self.lbl_focus, "guard off")
        elif ctl.focus.game_focused:
            self._set(self.lbl_focus, "yes", COLORS["good"])
        else:
            self._set(self.lbl_focus,
                      "NO -- input paused, click into Roblox" if running else "no",
                      COLORS["warn"] if running else "")

        text = "Stop  (F1)" if running else "Start  (F1)"
        if self.start_btn.text() != text:
            self.start_btn.setText(text)

        st = self.stats
        st["power"].setText(f"{s.power_percent}%" if s.power_percent not in ("", "---") else "---")
        st["progress"].setText(f"{s.progress_percent}%" if s.progress_percent != "" else "---")
        st["caught"].setText(
            f"{s.fish_caught_count} ({s.perfect_count} perfect)" if s.perfect_count
            else str(s.fish_caught_count))
        st["lost"].setText(str(s.fish_lost_count))
        rate = eng.fish_per_hour()
        st["rate"].setText(f"{rate:.0f}" if rate else "---")
        cycle = eng.average_cycle_seconds()
        st["cycle"].setText(f"{cycle:.1f}s" if cycle else "---")

        if self.detail.text() != ctl.detail:
            self.detail.setText(ctl.detail)
        if s.appraise_status and self.appraise_status.text() != s.appraise_status:
            self.appraise_status.setText(s.appraise_status)

    # -- window events ----------------------------------------------------

    def closeEvent(self, event) -> None:
        for timer in (self.pump_timer, self.refresh_timer, self.watch_timer):
            timer.stop()
        xlog.remove_listener(self._log_listener)
        self.ctl.shutdown()
        super().closeEvent(event)


def _qt_message_handler(mode, context, message) -> None:
    log.debug("qt: %s", message)


def run() -> int:
    from PySide6.QtCore import qInstallMessageHandler

    qInstallMessageHandler(_qt_message_handler)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("XTernal")
    screen = app.primaryScreen().geometry() if app.primaryScreen() else None
    size = (screen.width(), screen.height()) if screen else (0, 0)
    log.info("gui: Qt %s, primary screen %s, dpr %s", __import__("PySide6").__version__,
             size, app.primaryScreen().devicePixelRatio() if app.primaryScreen() else "?")
    ctl = Controller(screen_size=size)
    window = Window(ctl)
    ctl.start()
    window.show()
    window.raise_()
    return app.exec()
