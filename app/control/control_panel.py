"""PySide6 panel for a visible, persistent-per-session POC write enable."""
from __future__ import annotations
import json
import threading
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget
from app.config.settings import load_config
from app.llm.lm_studio_client import LmStudioClient
from .config import load_machine_config
from .service import ControlService

class PanelSignals(QObject):
    finished = Signal(object)

class ControlPanel(QWidget):
    def __init__(self, ads_provider, parent=None):
        super().__init__(parent)
        self.ads_provider = ads_provider
        self.cfg = load_config()
        self.machine_cfg = load_machine_config()
        self.signals = PanelSignals()
        self._build()
        self.signals.finished.connect(self._show_result)

    def _build(self):
        layout = QVBoxLayout(self)
        title = QLabel("POC: Benutzerbefehl -> lokales LLM -> gepruefter ADS-Schreibvorgang")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)
        self.write_enabled = QCheckBox("Schreibmodus freigegeben (bleibt aktiv, bis du ihn ausschaltest)")
        self.write_enabled.setChecked(False)
        self.write_enabled.setToolTip("Die Freigabe ist nur fuer diese laufende Anwendung aktiv.")
        layout.addWidget(self.write_enabled)
        box = QGroupBox("Benutzerbefehl")
        form = QFormLayout(box)
        self.command = QPlainTextEdit()
        self.command.setPlaceholderText("Beispiel: Ich will, dass der Zylinder A1 ausfaehrt")
        self.command.setMaximumHeight(90)
        form.addRow(self.command)
        self.send = QPushButton("Befehl an lokales LLM senden")
        self.send.clicked.connect(self._execute)
        form.addRow(self.send)
        layout.addWidget(box)
        layout.addWidget(QLabel("Entscheidung, Pruefungen und Ergebnis:"))
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result)
        layout.addStretch()

    def _execute(self):
        command = self.command.toPlainText().strip()
        ads = self.ads_provider()
        if ads is None or not getattr(ads, "connected", False):
            self.result.setPlainText("ADS ist nicht verbunden. Keine Aktion ausgefuehrt.")
            return
        if not hasattr(ads, "write_value"):
            self.result.setPlainText("Der aktuelle ADS-Client besitzt keinen Schreibpfad. Verwende WritableAdsClient.")
            return
        llm_cfg = self.cfg.get("llm", {})
        llm = LmStudioClient(
            base_url=llm_cfg.get("base_url", "http://127.0.0.1:1234/v1"),
            model=llm_cfg.get("model", "local-model"),
            timeout_seconds=float(llm_cfg.get("timeout_seconds", 120.0)),
            temperature=float(llm_cfg.get("temperature", 0.1)),
            max_tokens=int(llm_cfg.get("max_tokens", 1200)),
            context_length=int(llm_cfg.get("context_length", 4096)),
            top_p=float(llm_cfg.get("top_p", 0.95)),
            top_k=int(llm_cfg.get("top_k", 40)),
            repeat_penalty=float(llm_cfg.get("repeat_penalty", 1.1)),
            stream=False,
        )
        service = ControlService(ads, llm, self.machine_cfg)
        self.send.setEnabled(False)
        self.result.setPlainText("Snapshot wird gelesen und lokales LLM wird angefragt ...")
        enabled = self.write_enabled.isChecked()
        def worker():
            output = service.execute(command, enabled)
            self.signals.finished.emit(output)
        threading.Thread(target=worker, daemon=True).start()

    def _show_result(self, output):
        self.send.setEnabled(True)
        self.result.setPlainText(json.dumps(output, indent=2, ensure_ascii=False, default=str))
