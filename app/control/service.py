"""One-shot command flow: read -> ask local LLM -> validate -> write -> verify."""
from __future__ import annotations
from datetime import datetime, timezone
import time
from typing import Any
from .models import MachineConfig, ControlResponse
from .prompt import SYSTEM_PROMPT, build_control_prompt
from .validator import ValidationFailure, parse_response, validate_for_execution

class ControlService:
    def __init__(self, ads_client: Any, llm_client: Any, machine_config: MachineConfig):
        self.ads = ads_client
        self.llm = llm_client
        self.cfg = machine_config
        self.history: list[dict[str, Any]] = []
        self.write_timestamps: list[float] = []

    def _read_snapshot(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        snapshot: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for item in self.cfg.symbols:
            value, ok, error = self.ads.read_value(item.symbol, item.data_type)
            snapshot[item.symbol] = {
                "value": value if ok else None,
                "data_type": item.data_type,
                "role": item.role,
                "valid": bool(ok),
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            }
            if not ok:
                errors.append(f"{item.symbol}: {error}")
        return snapshot, errors

    def _prune_writes(self) -> None:
        cutoff = time.monotonic() - 60.0
        self.write_timestamps = [item for item in self.write_timestamps if item >= cutoff]

    def _verify_feedback(self, feedback: list[Any]) -> list[str]:
        errors: list[str] = []
        for expected in feedback:
            deadline = time.monotonic() + expected.timeout_seconds
            while time.monotonic() <= deadline:
                value, ok, error = self.ads.read_value(expected.symbol, self._data_type(expected.symbol))
                if ok and type(value) is type(expected.value) and value == expected.value:
                    break
                time.sleep(expected.poll_interval_seconds)
            else:
                errors.append(f"Erwartete Rueckmeldung nicht erreicht: {expected.symbol}={expected.value}")
        return errors

    def _data_type(self, symbol: str) -> str:
        for item in self.cfg.symbols:
            if item.symbol == symbol:
                return item.data_type
        raise KeyError(symbol)

    def execute(self, user_command: str, write_enabled: bool) -> dict[str, Any]:
        if not user_command.strip():
            return {"ok": False, "message": "Kein Benutzerbefehl eingegeben."}
        if not getattr(self.ads, "connected", False):
            return {"ok": False, "message": "ADS ist nicht verbunden. Keine Aktion ausgefuehrt."}
        before, errors = self._read_snapshot()
        if errors:
            return {"ok": False, "message": "Snapshot unvollstaendig. Keine Aktion ausgefuehrt.", "errors": errors}
        prompt = build_control_prompt(self.cfg, user_command.strip(), before, self.history)
        raw, llm_ok = self.llm.analyze(SYSTEM_PROMPT, prompt)
        if not llm_ok:
            return {"ok": False, "message": "Lokales LLM nicht verfuegbar. Keine Aktion ausgefuehrt.", "errors": [raw]}
        try:
            response: ControlResponse = parse_response(raw)
        except ValidationFailure as exc:
            return {"ok": False, "message": str(exc) + ". Keine Aktion ausgefuehrt."}
        after, errors = self._read_snapshot()
        if errors:
            return {"ok": False, "message": "Snapshot vor Ausfuehrung unvollstaendig. Keine Aktion ausgefuehrt.", "errors": errors, "response": response.model_dump(mode="json")}
        self._prune_writes()
        validation_errors = validate_for_execution(response, self.cfg, before, after, write_enabled, len(self.write_timestamps))
        if validation_errors:
            return {"ok": False, "message": "Pruefung nicht bestanden. Keine Aktion ausgefuehrt.", "errors": validation_errors, "response": response.model_dump(mode="json")}
        write_results: list[dict[str, Any]] = []
        for action in response.requested_actions:
            spec = next(item for item in self.cfg.symbols if item.symbol == action.symbol)
            ok, error = self.ads.write_value(action.symbol, spec.data_type, action.value)
            self.write_timestamps.append(time.monotonic())
            entry = {"symbol": action.symbol, "requested": action.value, "write_ok": ok, "error": error}
            if not ok:
                write_results.append(entry)
                return {"ok": False, "message": "ADS-Schreibfehler. Keine weitere Aktion ausgefuehrt.", "writes": write_results, "response": response.model_dump(mode="json")}
            actual, read_ok, read_error = self.ads.read_value(action.symbol, spec.data_type)
            entry.update({"actual": actual, "readback_ok": read_ok, "readback_error": read_error})
            if not read_ok or type(actual) is not type(action.value) or actual != action.value:
                write_results.append(entry)
                return {"ok": False, "message": "Ruecklesen weicht vom Schreibwert ab.", "writes": write_results, "response": response.model_dump(mode="json")}
            feedback_errors = self._verify_feedback(spec.expected_feedback)
            entry["feedback_errors"] = feedback_errors
            write_results.append(entry)
            if feedback_errors:
                return {"ok": False, "message": "Aktorwert gesetzt, erwartete Sensorreaktion blieb aus.", "writes": write_results, "response": response.model_dump(mode="json")}
        event = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "command": user_command, "response": response.model_dump(mode="json"), "writes": write_results}
        self.history.append(event)
        self.history = self.history[-30:]
        return {"ok": True, "message": "Befehl validiert, geschrieben und rueckgemeldet.", "writes": write_results, "response": response.model_dump(mode="json")}
