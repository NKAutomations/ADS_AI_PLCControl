from __future__ import annotations

import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .ads_client import AdsClient
from .config import APP_PATH, MACHINE_PATH, app_config, machine_config, save
from .control.agent_service import AgentControlService
from .llm_client import LlmClient

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
log = logging.getLogger(__name__)


def normalize_machine(data):
    """Keep older configurations compatible without widening the write path."""
    if not isinstance(data, dict):
        return data
    symbols = data.get("symbols", [])
    if isinstance(symbols, list):
        for spec in symbols:
            if isinstance(spec, dict) and spec.get("writable") is True:
                # Existing UI configurations use writable=true as the explicit
                # direction for the actuator. This does not grant new symbols.
                spec["role"] = "actuator"
    return data


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.app = app_config()
        self.machine = normalize_machine(machine_config())
        self.ads = None
        self.symbols = []
        self.write_enabled = False
        self.last_result = None
        self.progress = None
        self.command_running = False
        self.command_id = None
        self.command = None

    def llm(self):
        config = self.app.get("llm", {})
        return LlmClient(
            config.get("base_url", "http://127.0.0.1:1234/v1"),
            config.get("model", ""),
            float(config.get("timeout_seconds", 120)),
            float(config.get("temperature", 0.1)),
            int(config.get("max_tokens", 1200)),
            int(config.get("context_length", 4096)),
        )

    def public(self):
        return {
            "ads_connected": bool(self.ads and self.ads.connected),
            "write_enabled": self.write_enabled,
            "command_running": self.command_running,
            "command_id": self.command_id,
            "ads": self.app.get("ads", {}),
            "llm": self.app.get("llm", {}),
            "symbols": [item.__dict__ for item in self.symbols],
            "machine": self.machine,
            "progress": self.progress,
            "last_result": self.last_result,
        }


STATE = State()


def reply(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            with STATE.lock:
                return reply(self, STATE.public())
        if path == "/api/command-status":
            with STATE.lock:
                return reply(
                    self,
                    {
                        "ok": True,
                        "running": STATE.command_running,
                        "job_id": STATE.command_id,
                        "command": STATE.command,
                        "progress": STATE.progress,
                        "result": STATE.last_result,
                    },
                )
        if path == "/api/llm/check":
            ok, message = STATE.llm().check()
            return reply(self, {"ok": ok, "message": message})
        if path == "/":
            path = "/index.html"
        file_path = WEB / path.lstrip("/")
        if file_path.is_file():
            data = file_path.read_bytes()
            content_type = "text/html; charset=utf-8"
            if file_path.suffix == ".js":
                content_type = "text/javascript; charset=utf-8"
            elif file_path.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return reply(self, {"ok": False, "message": "Nicht gefunden"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
        except Exception as exc:
            return reply(self, {"ok": False, "message": f"Ungueltige Anfrage: {exc}"}, 400)

        with STATE.lock:
            if path == "/api/connect":
                config = data.get("ads", data)
                STATE.app.setdefault("ads", {}).update(config)
                save(APP_PATH, STATE.app)
                STATE.ads = AdsClient(
                    config.get("host", ""),
                    config.get("ams_net_id", ""),
                    int(config.get("port", 851)),
                    float(config.get("timeout_seconds", 3)),
                    int(config.get("notification_cycle_ms", 100)),
                )
                ok, message = STATE.ads.connect()
                return reply(self, {"ok": ok, "message": message})

            if path == "/api/disconnect":
                if STATE.command_running:
                    return reply(self, {"ok": False, "message": "Auftrag laeuft noch."}, 409)
                if STATE.ads:
                    STATE.ads.disconnect()
                STATE.write_enabled = False
                return reply(self, {"ok": True})

            if path == "/api/symbols":
                if not STATE.ads or not STATE.ads.connected:
                    return reply(self, {"ok": False, "message": "ADS nicht verbunden"}, 400)
                STATE.symbols, error = STATE.ads.read_all_symbols()
                return reply(
                    self,
                    {
                        "ok": not bool(error),
                        "message": error,
                        "symbols": [item.__dict__ for item in STATE.symbols],
                    },
                )

            if path == "/api/machine":
                if not isinstance(data, dict):
                    return reply(self, {"ok": False, "message": "Maschinenbeschreibung muss ein Objekt sein."}, 400)
                STATE.machine = normalize_machine(data)
                save(MACHINE_PATH, STATE.machine)
                return reply(self, {"ok": True, "machine": STATE.machine})

            if path == "/api/settings":
                if isinstance(data.get("ads"), dict):
                    STATE.app.setdefault("ads", {}).update(data["ads"])
                if isinstance(data.get("llm"), dict):
                    STATE.app.setdefault("llm", {}).update(data["llm"])
                save(APP_PATH, STATE.app)
                return reply(self, {"ok": True, "ads": STATE.app.get("ads", {}), "llm": STATE.app.get("llm", {})})

            if path == "/api/write-enable":
                if STATE.command_running and not bool(data.get("enabled", False)):
                    STATE.write_enabled = False
                else:
                    STATE.write_enabled = bool(data.get("enabled", False))
                return reply(self, {"ok": True, "write_enabled": STATE.write_enabled})

            if path == "/api/read":
                values = []
                for spec in STATE.machine.get("symbols", []):
                    if STATE.ads and STATE.ads.connected:
                        value, ok, error = STATE.ads.read_value(spec["symbol"], spec["data_type"])
                        values.append({
                            "symbol": spec["symbol"],
                            "value": value if ok else None,
                            "valid": ok,
                            "error": error,
                        })
                return reply(self, {"ok": True, "values": values})

            if path == "/api/command":
                if not STATE.ads or not STATE.ads.connected:
                    return reply(self, {"ok": False, "message": "ADS nicht verbunden"}, 400)
                command = str(data.get("command", "")).strip()
                if not command:
                    return reply(self, {"ok": False, "message": "Kein Befehl eingegeben."}, 400)
                if STATE.command_running:
                    return reply(self, {"ok": False, "message": "Es laeuft bereits ein Auftrag."}, 409)

                if isinstance(data.get("llm"), dict):
                    STATE.app.setdefault("llm", {}).update(data["llm"])
                    save(APP_PATH, STATE.app)

                job_id = uuid.uuid4().hex
                ads = STATE.ads
                llm = STATE.llm()
                machine = json.loads(json.dumps(STATE.machine))
                # Die Bedienerfreigabe wird bei jedem weiteren Agentenschritt
                # erneut gelesen. Sie kann damit auch waehrend eines laufenden
                # Auftrags widerrufen werden.
                write_enabled = lambda: STATE.write_enabled
                STATE.command_running = True
                STATE.command_id = job_id
                STATE.command = command
                STATE.progress = {
                    "event": "started",
                    "job_id": job_id,
                    "command": command,
                    "status": "running",
                    "step": 0,
                    "steps": [],
                }
                STATE.last_result = None

                def progress(event):
                    with STATE.lock:
                        current = STATE.progress or {"steps": []}
                        steps = current.setdefault("steps", [])
                        if event.get("event") in {"decision_received", "step_executed", "next_decision_pending", "wait"}:
                            steps.append(event)
                        STATE.progress = {
                            **current,
                            "event": event.get("event", "progress"),
                            "step": event.get("step", current.get("step", 0)),
                            "last_event": event,
                        }

                def run_command():
                    try:
                        service = AgentControlService(ads, llm, machine, progress_callback=progress)
                        result = service.execute(command, write_enabled, job_id=job_id)
                    except Exception as exc:
                        log.exception("Unerwarteter Fehler bei der Auftragsausfuehrung")
                        result = {
                            "ok": False,
                            "status": "failed",
                            "job_id": job_id,
                            "message": "Unerwarteter Fehler. Keine weitere Aktion ausgefuehrt.",
                            "errors": [str(exc)],
                        }
                    with STATE.lock:
                        STATE.last_result = result
                        STATE.progress = {
                            **(STATE.progress or {}),
                            "event": "finished",
                            "status": result.get("status", "failed"),
                            "step": result.get("step_count", 0),
                        }
                        STATE.command_running = False

                threading.Thread(target=run_command, name=f"ads-agent-{job_id[:8]}", daemon=True).start()
                return reply(
                    self,
                    {
                        "ok": True,
                        "started": True,
                        "job_id": job_id,
                        "message": "Auftrag gestartet. Der Zustand wird nach jedem Schritt neu bewertet.",
                    },
                )

        return reply(self, {"ok": False, "message": "Nicht gefunden"}, 404)


def run():
    config = STATE.app.get("server", {})
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8080))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("Weboberflaeche gestartet: http://%s:%s", host, port)
    server.serve_forever()
