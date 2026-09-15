from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QCheckBox, QPlainTextEdit, QFormLayout, QDialog,
    QDialogButtonBox, QFileDialog, QMessageBox, QScrollArea)

from .rhythm_diagnostic import (DISPLAY_KEYS, average, read_samples, report_text,
    save_session, timestamp, validate_metrics)
from .rhythm_environment import CpuSampler, capture_environment, foreground_target, keyboard_devices


class ResultDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("輸入本局結果（空白表示未取得）")
        self.metrics = {}
        self.samples = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("依遊戲結果填寫；accuracy 用 0–100%，offset 單位 ms。"))
        form = QFormLayout()
        self.fields = {}
        for key, label in [("accuracy", "Accuracy %"), ("perfect", "Perfect"), ("great", "Great"),
                ("good", "Good"), ("miss", "Miss"), ("total_notes", "總判定數（計算 Miss rate）"),
                ("fast", "Fast"), ("slow", "Slow"), ("timing_bias_ms", "平均 offset ms（正=慢）"),
                ("timing_sd_ms", "Timing SD ms"), ("frametime_p99_ms", "Frametime P99 ms")]:
            field = QLineEdit()
            self.fields[key] = field
            form.addRow(label, field)
        layout.addLayout(form)
        self.source = QLineEdit()
        self.source.setPlaceholderText("例如：遊戲結果畫面、PresentMon 本局匯出")
        layout.addWidget(self.source)
        self.import_button = QPushButton("匯入本局 CSV 樣本（選用）")
        self.import_button.clicked.connect(self.import_samples)
        layout.addWidget(self.import_button)
        layout.addWidget(QLabel("欄位：frametime_ms / offset_ms / keydown_timestamp_ms\n只匯入本局有效遊玩區段；CSV 樣本會優先於手填統計。"))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def import_samples(self):
        path, _ = QFileDialog.getOpenFileName(self, "本局 CSV", "", "CSV (*.csv)")
        if path:
            try:
                self.samples = read_samples(path)
                self.import_button.setText(f"已匯入：{Path(path).name}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "匯入失敗", str(exc))

    def validate(self):
        try:
            self.metrics = {k: float(v.text()) for k, v in self.fields.items() if v.text().strip()}
            validate_metrics(self.metrics)
            if not self.metrics and not self.samples:
                raise ValueError("至少填一項結果或匯入樣本")
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "請檢查結果", str(exc))


class RhythmDiagnosticWidget(QWidget):
    status_changed = Signal(str)

    def __init__(self, storage: Path, snapshot_provider, parent=None):
        super().__init__(parent)
        self.storage = Path(storage)
        self.snapshot_provider = snapshot_provider
        self.active = None
        self.pending = False
        self.session = {"version": 1, "runs": []}
        self.path = None
        self.sampler = None
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.arm_timer = QTimer(self)
        self.arm_timer.setSingleShot(True)
        self.arm_timer.timeout.connect(self.begin)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        help_label = QLabel("同一首／同一譜面，交錯 A/B 各至少 3 局。每次開始後有 5 秒切回遊戲。\n"
            "保持音訊、遊戲 offset、難度、速度、鍵盤、FPS 上限與背景程式一致；每次只改一項。\n"
            "全程只讀取環境；不攔截按鍵。結果採遊戲設定檔＋手填／CSV，不使用 OCR。")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        form = QFormLayout()
        self.profile = QLineEdit()
        self.profile.setPlaceholderText("遊戲名稱／版本；判定與 offset 換算規則")
        self.chart = QLineEdit()
        self.chart.setPlaceholderText("歌曲、譜面、難度、速度（同一測試不可變更）")
        self.setting_a = QLineEdit("原生解析度")
        self.setting_b = QLineEdit("替代解析度／縮放")
        self.region = QLineEdit()
        self.region.setPlaceholderText("選用：結果區域 x,y,width,height（遊戲 client 像素）；供後續遊戲擴充")
        self.setting = QComboBox()
        self.setting.addItems(["A", "B"])
        self.mode = QComboBox()
        self.mode.addItems(["unknown", "exclusive_fullscreen", "borderless", "windowed"])
        self.threshold = QLineEdit("25")
        for label, field in [("遊戲設定檔", self.profile), ("譜面", self.chart), ("設定 A", self.setting_a),
                ("設定 B", self.setting_b), ("結果區域（選用）", self.region), ("本局設定", self.setting),
                ("遊戲選單顯示模式（手動確認）", self.mode), ("Spike 門檻 ms（全測試固定）", self.threshold)]:
            form.addRow(label, field)
        layout.addLayout(form)
        self.controls = QCheckBox("已確認其他條件保持一致，結果 offset 已統一正=慢／負=快")
        layout.addWidget(self.controls)
        self.state = QLabel("尚未記錄")
        layout.addWidget(self.state)
        row = QHBoxLayout()
        self.start_button = QPushButton("開始本局（5 秒後鎖定遊戲）")
        self.stop_button = QPushButton("結束本局並填結果")
        self.stop_button.setEnabled(False)
        self.cancel_button = QPushButton("取消本局")
        self.cancel_button.setEnabled(False)
        self.start_button.clicked.connect(self.arm)
        self.stop_button.clicked.connect(self.finish)
        self.cancel_button.clicked.connect(self.cancel)
        for button in (self.start_button, self.stop_button, self.cancel_button):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.new_button = QPushButton("新 A/B 測試")
        self.load_button = QPushButton("開啟本機測試")
        self.profile_button = QPushButton("載入遊戲設定檔")
        self.save_profile_button = QPushButton("儲存遊戲設定檔")
        self.new_button.clicked.connect(self.new_session)
        self.load_button.clicked.connect(self.load)
        self.profile_button.clicked.connect(self.load_profile)
        self.save_profile_button.clicked.connect(self.save_profile)
        for button in (self.new_button, self.load_button, self.profile_button, self.save_profile_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)
        self.update_report()

    def set_state(self, text):
        self.state.setText(text)
        self.status_changed.emit(text)

    def metadata_widgets(self):
        return (self.profile, self.chart, self.setting_a, self.setting_b, self.region, self.threshold, self.controls)

    def lock(self):
        busy = self.pending or self.active is not None
        for field in self.metadata_widgets():
            field.setEnabled(not busy and not self.session["runs"])
        for field in (self.setting, self.mode, self.new_button, self.load_button, self.profile_button, self.save_profile_button):
            field.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(self.active is not None)
        self.cancel_button.setEnabled(busy)

    def profile_data(self):
        region = [int(x.strip()) for x in self.region.text().split(",")] if self.region.text().strip() else None
        if region is not None and (len(region) != 4 or min(region[:2]) < 0 or min(region[2:]) <= 0):
            raise ValueError("結果區域格式：x,y,width,height，寬高需大於 0")
        return {"name": self.profile.text().strip(), "result_region_client_px": region,
                "result_method": "manual_or_csv", "offset_convention": "positive_late_ms"}

    def arm(self):
        try:
            profile = self.profile_data()
            threshold = float(self.threshold.text())
            if not 0 < threshold < 10000:
                raise ValueError("Spike 門檻需介於 0 與 10000 ms")
            if not profile["name"] or not self.chart.text().strip():
                raise ValueError("請先填寫遊戲設定檔與譜面")
            self.session.update(profile=profile, chart=self.chart.text().strip(),
                settings={"A": self.setting_a.text(), "B": self.setting_b.text()},
                controls_confirmed=self.controls.isChecked(), spike_threshold_ms=threshold)
            if self.path is None:
                self.path = self.storage / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
            self.pending = True
            self.lock()
            snapshot = self.snapshot_provider()
            name = getattr(snapshot, "app_name", "未辨識")
            self.set_state(f"音遊診斷：5 秒內切回遊戲（活動監控：{name}）")
            self.arm_timer.start(5000)
        except ValueError as exc:
            QMessageBox.warning(self, "請檢查設定", str(exc))

    def begin(self):
        self.pending = False
        try:
            target = foreground_target()
            if target["pid"] == os.getpid():
                raise ValueError("前景仍是助理，請重新開始並切回遊戲")
            environment = capture_environment(target)
            if not environment.get("client_px"):
                raise ValueError("無法取得遊戲視窗，請重新選擇")
            if self.session["runs"] and target["app"] != self.session["runs"][0]["environment"]["app"]:
                raise ValueError("遊戲程式與前幾局不同；請建立新測試")
            environment["declared_presentation"] = self.mode.currentText()
            try:
                environment["keyboard_devices"] = keyboard_devices()
            except Exception:
                environment["keyboard_devices"] = []
            self.active = {"setting": self.setting.currentText(), "chart": self.session["chart"],
                "started_at": timestamp(), "environment": environment, "cpu_samples": [],
                "spike_threshold_ms": self.session["spike_threshold_ms"], "environment_changed": False}
            try:
                self.sampler = CpuSampler(target["pid"])
            except Exception:
                self.sampler = None
            self.timer.start()
            self.set_state(f"音遊診斷：正在記錄 {self.setting.currentText()}／{target['app']}")
        except Exception as exc:
            self.set_state(f"未開始：{exc}")
        self.lock()

    def tick(self):
        if self.active is None:
            return
        if self.sampler:
            self.active["cpu_samples"].append(self.sampler.sample())
        current = capture_environment(self.active["environment"])
        # Exclusive games may restore desktop mode on Alt-Tab; that is not an in-game change.
        if current.get("is_foreground", True) and not current.get("minimized", False) and any(
                current.get(k) != self.active["environment"].get(k) for k in DISPLAY_KEYS):
            self.active["environment_changed"] = True
            observation = {k: current.get(k) for k in DISPLAY_KEYS}
            changes = self.active.setdefault("environment_changes", [])
            if not changes or changes[-1]["display"] != observation:
                changes.append({"at": timestamp(), "display": observation})

    def cancel(self):
        self.arm_timer.stop()
        self.timer.stop()
        self.pending = False
        self.active = None
        self.sampler = None
        self.set_state("音遊診斷：本局已取消")
        self.lock()

    def finish(self):
        if self.active is None:
            return
        self.timer.stop()
        self.active.setdefault("ended_at", timestamp())
        self.set_state("音遊診斷：已停止記錄，等待填寫結果")
        dialog = ResultDialog(self)
        if dialog.exec() != QDialog.Accepted:
            self.set_state("音遊診斷：本局已停止，按結束可繼續填結果，或取消本局")
            return
        run = dict(self.active)
        run.update(metrics=dialog.metrics, samples=dialog.samples, result_source=dialog.source.text() or "manual")
        run["metrics"]["system_cpu_percent"] = average([s["system_cpu_percent"] for s in run["cpu_samples"] if s.get("system_cpu_percent") is not None])
        run["metrics"]["game_cpu_percent"] = average([s["game_cpu_percent"] for s in run["cpu_samples"] if s.get("game_cpu_percent") is not None])
        candidate = {**self.session, "runs": [*self.session["runs"], run]}
        try:
            save_session(self.path, candidate)
        except OSError as exc:
            QMessageBox.warning(self, "儲存失敗（本局仍保留）", str(exc))
            return
        self.session = candidate
        self.active = None
        self.sampler = None
        self.lock()
        self.set_state("音遊診斷：本局已儲存")
        self.update_report()

    def update_report(self):
        text = report_text(self.session)
        if self.path:
            text += f"\n\n本機資料：{self.path}"
        if self.session["runs"]:
            text += "\n\n最近一局環境：\n" + json.dumps(self.session["runs"][-1]["environment"], ensure_ascii=False, indent=2)
        self.output.setPlainText(text)

    def new_session(self):
        self.session = {"version": 1, "runs": []}
        self.path = None
        self.lock()
        self.update_report()
        self.set_state("音遊診斷：新測試，舊資料保留在本機")

    def apply_profile(self, data):
        self.profile.setText(data["name"])
        self.region.setText(",".join(map(str, data.get("result_region_client_px") or [])))

    def save_profile(self):
        try:
            data = self.profile_data()
            path, _ = QFileDialog.getSaveFileName(self, "儲存遊戲設定檔", "rhythm_profile.json", "JSON (*.json)")
            if path:
                save_session(path, data)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "設定檔錯誤", str(exc))

    def load_profile(self):
        if self.session["runs"]:
            QMessageBox.information(self, "設定檔", "請先建立新 A/B 測試再更換設定檔")
            return
        path, _ = QFileDialog.getOpenFileName(self, "遊戲設定檔", "", "JSON (*.json)")
        if path:
            try:
                self.apply_profile(json.loads(Path(path).read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                QMessageBox.warning(self, "設定檔錯誤", str(exc))

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "本機 A/B 測試", str(self.storage), "JSON (*.json)")
        if not path:
            return
        try:
            session = json.loads(Path(path).read_text(encoding="utf-8"))
            if session.get("version") != 1 or not isinstance(session.get("runs"), list):
                raise ValueError("不是支援的音遊測試檔")
            for run in session["runs"]:
                validate_metrics(run["metrics"])
            report_text(session)
            self.apply_profile(session["profile"])
            self.chart.setText(session["chart"])
            self.setting_a.setText(session["settings"]["A"])
            self.setting_b.setText(session["settings"]["B"])
            self.controls.setChecked(session["controls_confirmed"])
            self.threshold.setText(str(session["spike_threshold_ms"]))
            self.session, self.path = session, Path(path)
            self.lock()
            self.update_report()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "無法開啟", str(exc))
