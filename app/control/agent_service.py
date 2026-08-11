"""Bounded multi-step control service for the existing web POC.

This module intentionally keeps the existing ADS and machine configuration
interfaces. It adds a controlled job loop without replacing the existing
one-shot ControlService.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


DEFAULT_LIMITS = {
    "max_steps": 8,
    "max_writes_per_job": 8,
    "max_writes_per_minute": 10,
    "job_timeout_seconds": 120.0,
    "max_wait_seconds": 10.0,
    "max_identical_decisions": 2,
}

DECISIONS = {"continue", "completed", "wait", "blocked", "fault", "unclear"}
MACHINE_STATES = {"unbekannt", "bereit", "in_ausfuehrung", "erreicht", "stoerung", "pruefen"}

AGENT_SYSTEM_PROMPT = """Du bist die lokale Vorschlagskomponente einer kontrollierten TwinCAT-POC-Anwendung.
Du hast keinen ADS-Zugriff. Du darfst keine freien ADS-Symbolnamen erzeugen.
Verwende nur Symbole, Datentypen und Werte aus der Maschinenbeschreibung.

Du entscheidest immer nur ueber den unmittelbar naechsten Schritt. Plane keine
zukuenftigen Schritte vor und fordere maximal eine Schreibaktion an.
Die Python-Anwendung validiert deine Antwort deterministisch und entscheidet
endgueltig, ob geschrieben wird.

Antworte ausschliesslich mit genau einem JSON-Objekt, ohne Markdown und ohne
Codeblock. Verwende exakt diese Felder:
{
  "timestamp": "ISO-8601 mit Zeitzone",
  "decision": "continue|completed|wait|blocked|fault|unclear",
  "read_only": true,
  "machine_state": "unbekannt|bereit|in_ausfuehrung|erreicht|stoerung|pruefen",
  "confidence": 0.0,
  "observations": ["relevante Beobachtung"],
  "anomalies": ["relevante Abweichung"],
  "requested_actions": [{"symbol":"exakter Symbolname","value":true,"reason":"konkrete Begruendung"}],
  "completion_checks": [{"symbol":"exakter Symbolname","value":true}],
  "wait": false,
  "wait_seconds": 0.0,
  "safe_state_required": false,
  "summary": "Kurze Zusammenfassung der Entscheidung"
}

Regeln:
- Nach jeder ausgefuehrten continue-Aktion muss der naechste reale Snapshot bewertet werden.
- completed darf niemals direkt nur aus der vorherigen Schreibaktion abgeleitet werden.
- Bei completed muss completion_checks mindestens eine konkrete SPS-Pruefung enthalten.
- Bei completed, wait, blocked, fault oder unclear ist requested_actions leer.
- Bei continue muss requested_actions genau eine Aktion enthalten.
- Bei fehlenden Daten, fehlender Freigabe, Stoerung oder Unklarheit niemals schreiben.
- Die Schreibfreigabe darf niemals durch dich aktiviert werden.
- wait_seconds darf nur bei wait groesser als null sein.
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _json_value(value: Any) -> Any:
    """Make values safe for the API result without changing validation."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


class AgentControlService:
    """Execute one user command as a bounded sequence of validated steps."""

    def __init__(
        self,
        ads: Any,
        llm: Any,
        machine: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ads = ads
        self.llm = llm
        self.machine = machine
        self.progress_callback = progress_callback
        self.sleep = sleep
        self.write_times: list[float] = []
        self.history: list[dict[str, Any]] = []
        self.job_id = ""

    def _limits(self) -> dict[str, float | int]:
        configured = self.machine.get("agent", {})
        result: dict[str, float | int] = dict(DEFAULT_LIMITS)
        if isinstance(configured, dict):
            for key, default in DEFAULT_LIMITS.items():
                value = configured.get(key, default)
                try:
                    result[key] = float(value) if isinstance(default, float) else int(value)
                except (TypeError, ValueError):
                    result[key] = default
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        self.history.append(event)
        self.history = self.history[-30:]
        if self.progress_callback is not None:
            self.progress_callback(event)

    def _spec(self, symbol: str) -> dict[str, Any] | None:
        for item in self.machine.get("symbols", []):
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return item
        return None

    def _snapshot(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        values: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for item in self.machine.get("symbols", []):
            symbol = item.get("symbol")
            data_type = item.get("data_type")
            value, ok, error = self.ads.read_value(symbol, data_type)
            values[symbol] = {
                "value": _json_value(value) if ok else None,
                "data_type": data_type,
                "role": item.get("role", "state"),
                "description": item.get("description", ""),
                "valid": bool(ok),
                "timestamp": _now(),
            }
            if not ok:
                errors.append(f"{symbol}: {error}")
        return values, errors

    def _prompt(self, command: str, snapshot: dict[str, dict[str, Any]]) -> str:
        payload = {
            "job_id": self.job_id,
            "original_command": command,
            "machine_description": self.machine,
            "snapshot_before_decision": snapshot,
            "previous_steps": self.history[-30:],
        }
        return (
            "ENTSCHEIDE NUR DEN UNMITTELBAR NAECHSTEN SCHRITT.\n"
            "Der reale SPS-Zustand ist massgeblich.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def _ask(self, prompt: str) -> tuple[str, bool]:
        # The current repository client exposes ask(prompt). A future client
        # may expose ask_agent(prompt, system_prompt); both are supported.
        if hasattr(self.llm, "ask_agent"):
            return self.llm.ask_agent(prompt, AGENT_SYSTEM_PROMPT)
        return self.llm.ask(prompt)

    def _parse_response(self, raw: str) -> tuple[dict[str, Any] | None, list[str]]:
        if not isinstance(raw, str) or not raw.strip():
            return None, ["LLM-Antwort ist leer"]
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, [f"LLM-Antwort ist kein gueltiges JSON: {exc.msg}"]
        if not isinstance(response, dict):
            return None, ["LLM-Antwort muss ein JSON-Objekt sein"]

        required = {
            "timestamp", "decision", "read_only", "machine_state", "confidence",
            "observations", "anomalies", "requested_actions", "wait",
            "wait_seconds", "safe_state_required", "summary",
        }
        errors = [f"Pflichtfeld fehlt: {key}" for key in sorted(required - set(response))]
        # completion_checks is required only for a claimed completion. This
        # keeps the existing one-shot response compatible for intermediate
        # decisions while keeping the final state proof fail-closed.
        if "completion_checks" not in response:
            response["completion_checks"] = []
        errors.extend(f"Unerlaubtes Feld: {key}" for key in sorted(set(response) - required))
        if errors:
            return response, errors

        if response["decision"] not in DECISIONS:
            errors.append("Entscheidung ist nicht erlaubt")
        if response["machine_state"] not in MACHINE_STATES:
            errors.append("Maschinenzustand ist nicht erlaubt")
        if type(response["read_only"]) is not bool or type(response["wait"]) is not bool:
            errors.append("read_only und wait muessen bool sein")
        if type(response["safe_state_required"]) is not bool:
            errors.append("safe_state_required muss bool sein")
        if type(response["confidence"]) not in {int, float} or isinstance(response["confidence"], bool):
            errors.append("Konfidenz ist ungueltig")
        elif not 0 <= response["confidence"] <= 1:
            errors.append("Konfidenz muss zwischen 0 und 1 liegen")
        if not isinstance(response["observations"], list) or not all(isinstance(x, str) for x in response["observations"]):
            errors.append("observations muss eine Liste von Texten sein")
        if not isinstance(response["anomalies"], list) or not all(isinstance(x, str) for x in response["anomalies"]):
            errors.append("anomalies muss eine Liste von Texten sein")
        if not isinstance(response["summary"], str) or not response["summary"].strip():
            errors.append("summary darf nicht leer sein")
        checks = response["completion_checks"]
        if not isinstance(checks, list) or len(checks) > 20:
            errors.append("completion_checks muss eine Liste mit maximal 20 Eintraegen sein")
        if response["decision"] == "completed" and not checks:
            errors.append("completed erfordert mindestens eine konkrete SPS-Abschlusspruefung")
        elif not all(isinstance(item, dict) and set(item) == {"symbol", "value"} and isinstance(item["symbol"], str) and item["symbol"] for item in checks):
            errors.append("completion_checks muss exakt symbol und value enthalten")
        if type(response["wait_seconds"]) not in {int, float} or isinstance(response["wait_seconds"], bool):
            errors.append("wait_seconds ist ungueltig")
        elif response["wait_seconds"] < 0:
            errors.append("wait_seconds darf nicht negativ sein")

        actions = response["requested_actions"]
        actions_valid = isinstance(actions, list)
        if not actions_valid:
            errors.append("requested_actions muss eine Liste sein")
            actions = []
        elif len(actions) > 1:
            errors.append("Pro Entscheidung ist maximal eine Aktion erlaubt")
        elif actions:
            action = actions[0]
            if not isinstance(action, dict) or set(action) != {"symbol", "value", "reason"}:
                errors.append("Aktion muss exakt symbol, value und reason enthalten")
            elif not isinstance(action["symbol"], str) or not action["symbol"]:
                errors.append("Aktionssymbol ist ungueltig")
            elif not isinstance(action["reason"], str) or not action["reason"].strip():
                errors.append("Aktionsbegruendung darf nicht leer sein")

        decision = response["decision"]
        if decision == "continue" and len(actions) != 1:
            errors.append("continue muss genau eine Aktion enthalten")
        if decision != "continue" and actions:
            errors.append("Nur continue darf eine Schreibaktion enthalten")
        if decision == "wait" and response["wait_seconds"] <= 0:
            errors.append("wait muss eine positive Wartezeit enthalten")
        if decision != "wait" and response["wait_seconds"] != 0:
            errors.append("wait_seconds darf nur bei wait gesetzt sein")
        if decision == "wait" and response["wait"] is not True:
            errors.append("wait muss bei decision=wait true sein")
        if decision != "wait" and response["wait"] is not False:
            errors.append("wait muss ausserhalb von wait false sein")
        if bool(actions) and response["read_only"] is not False:
            errors.append("Eine Schreibaktion erfordert read_only=false")
        if not actions and response["read_only"] is not True:
            errors.append("Eine Entscheidung ohne Aktion erfordert read_only=true")
        return response, errors

    def _validate_timestamp_and_confidence(self, response: dict[str, Any], limits: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            stamp = datetime.fromisoformat(str(response["timestamp"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                errors.append("Zeitstempel besitzt keine Zeitzone")
            else:
                age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
                if age < -5:
                    errors.append("Zeitstempel liegt unzulaessig in der Zukunft")
                elif age > float(self.machine.get("max_response_age_seconds", 15)):
                    errors.append("LLM-Antwort ist veraltet")
        except (TypeError, ValueError):
            errors.append("Zeitstempel ist ungueltig")
        if response["confidence"] < float(self.machine.get("min_confidence", 0.85)):
            errors.append("Konfidenz ist zu niedrig")
        if response["wait_seconds"] > float(limits["max_wait_seconds"]):
            errors.append("Wartezeit ueberschreitet das konfigurierte Limit")
        return errors

    def _execution_conditions(self, snapshot: dict[str, dict[str, Any]]) -> list[str]:
        errors: list[str] = []
        execution = self.machine.get("execution", {})
        for symbol in execution.get("required_true", []):
            if snapshot.get(symbol, {}).get("value") is not True:
                errors.append(f"Freigabe fehlt: {symbol}")
        for symbol in execution.get("required_false", []):
            if snapshot.get(symbol, {}).get("value") is not False:
                errors.append(f"Verriegelung aktiv oder ungueltig: {symbol}")
        mode_symbol = execution.get("mode_symbol")
        allowed_modes = execution.get("allowed_modes", [])
        if mode_symbol and allowed_modes:
            mode_values = execution.get("mode_values", {})
            mode_name = mode_values.get(str(snapshot.get(mode_symbol, {}).get("value")))
            if mode_name not in allowed_modes:
                errors.append(f"Betriebsart nicht freigegeben: {mode_name}")
        return errors

    def _type_ok(self, spec: dict[str, Any], value: Any) -> bool:
        data_type = spec.get("data_type")
        if data_type == "BOOL":
            return type(value) is bool
        if data_type in {"INT", "DINT", "UINT", "UDINT", "TIME"}:
            return type(value) is int
        if data_type in {"REAL", "LREAL"}:
            return type(value) in {int, float} and not isinstance(value, bool)
        if data_type == "STRING":
            return type(value) is str
        return False

    def _validate_action(
        self,
        response: dict[str, Any],
        before: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        write_enabled: bool | Callable[[], bool],
        limits: dict[str, Any],
    ) -> list[str]:
        errors = self._validate_timestamp_and_confidence(response, limits)
        current_write_enabled = bool(write_enabled() if callable(write_enabled) else write_enabled)
        if self.machine.get("enabled") is not True:
            errors.append("Maschinenbeschreibung ist deaktiviert")
        action = response["requested_actions"][0]
        symbol = action["symbol"]
        spec = self._spec(symbol)
        if spec is None:
            errors.append(f"Symbol nicht in Maschinenbeschreibung: {symbol}")
            return errors
        if current_write_enabled is not True:
            errors.append("Schreibmodus ist in der Weboberflaeche nicht freigegeben")
        if spec.get("writable") is not True or spec.get("role") not in {"actuator", None}:
            errors.append(f"Symbol nicht als schreibbarer Aktor freigegeben: {symbol}")
        if not current.get(symbol, {}).get("valid"):
            errors.append(f"Aktionssymbol ist im aktuellen Snapshot ungueltig: {symbol}")
        if not self._type_ok(spec, action["value"]):
            errors.append(f"Datentyp passt nicht: {symbol}")
        allowed = spec.get("allowed_values")
        if allowed and not any(_same_value(action["value"], item) for item in allowed):
            errors.append(f"Wert nicht erlaubt: {symbol}")
        errors.extend(self._execution_conditions(current))

        # Compare every configured signal again immediately before writing.
        for symbol_name, state in before.items():
            new_state = current.get(symbol_name, {})
            if not new_state.get("valid") or not _same_value(state.get("value"), new_state.get("value")):
                errors.append(f"Anlagenzustand hat sich unerwartet geaendert: {symbol_name}")
        self._prune_writes()
        if len(self.write_times) + 1 > int(limits["max_writes_per_minute"]):
            errors.append("Maximale Schreibfrequenz ueberschritten")
        return errors

    def _prune_writes(self) -> None:
        cutoff = time.monotonic() - 60.0
        self.write_times = [stamp for stamp in self.write_times if stamp >= cutoff]

    def _feedback(self, spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for expected in spec.get("expected_feedback", []) or []:
            target = self._spec(expected.get("symbol"))
            if target is None:
                errors.append(f"Feedbacksymbol nicht beschrieben: {expected.get('symbol')}")
                continue
            timeout = float(expected.get("timeout_seconds", 5.0))
            interval = float(expected.get("poll_interval_seconds", 0.1))
            deadline = time.monotonic() + timeout
            reached = False
            last_error = ""
            while time.monotonic() <= deadline:
                value, ok, error = self.ads.read_value(target["symbol"], target["data_type"])
                last_error = error
                if ok and _same_value(value, expected.get("value")):
                    reached = True
                    break
                self.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            if not reached:
                suffix = f": {last_error}" if last_error else ""
                errors.append(f"Feedback nicht erreicht: {expected.get('symbol')}={expected.get('value')}{suffix}")
        return errors

    def _fingerprint(self, response: dict[str, Any]) -> str:
        return json.dumps(
            {
                "decision": response["decision"],
                "actions": response["requested_actions"],
                "wait": response["wait"],
                "wait_seconds": response["wait_seconds"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def execute(
        self,
        command: str,
        write_enabled: bool | Callable[[], bool],
        job_id: str | None = None,
    ) -> dict[str, Any]:
        command = str(command or "").strip()
        if not command:
            return {"ok": False, "status": "failed", "message": "Kein Benutzerbefehl eingegeben."}
        if not getattr(self.ads, "connected", False):
            return {"ok": False, "status": "failed", "message": "ADS ist nicht verbunden. Keine Aktion ausgefuehrt."}

        self.job_id = job_id or uuid.uuid4().hex
        limits = self._limits()
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        writes = 0
        repeated: dict[str, int] = {}

        snapshot, errors = self._snapshot()
        if errors:
            return {
                "ok": False, "status": "failed", "job_id": self.job_id,
                "message": "Snapshot unvollstaendig. Keine Aktion ausgefuehrt.",
                "errors": errors, "steps": steps,
            }

        for step_index in range(1, int(limits["max_steps"]) + 1):
            if time.monotonic() - started > float(limits["job_timeout_seconds"]):
                return self._finish(False, "failed", "Auftragszeitlimit ueberschritten.", steps, writes)

            raw, llm_ok = self._ask(self._prompt(command, snapshot))
            step: dict[str, Any] = {
                "step": step_index,
                "started_at": _now(),
                "snapshot_before": snapshot,
                "llm_raw": raw if isinstance(raw, str) else repr(raw),
            }
            if not llm_ok:
                step["status"] = "failed"
                step["errors"] = [str(raw)]
                steps.append(step)
                return self._finish(False, "failed", "Lokales LLM nicht verfuegbar. Keine Aktion ausgefuehrt.", steps, writes)

            response, parse_errors = self._parse_response(raw)
            if response is None or parse_errors:
                step["status"] = "failed"
                step["errors"] = parse_errors
                step["response"] = response
                steps.append(step)
                return self._finish(False, "failed", "LLM-Antwort wurde strikt abgelehnt. Keine Aktion ausgefuehrt.", steps, writes)
            step["response"] = response
            self._emit({
                "event": "decision_received",
                "job_id": self.job_id,
                "step": step_index,
                "decision": response,
            })

            repeated_key = self._fingerprint(response)
            repeated[repeated_key] = repeated.get(repeated_key, 0) + 1
            if repeated[repeated_key] > int(limits["max_identical_decisions"]):
                step["status"] = "failed"
                step["errors"] = ["Wiederholte identische KI-Entscheidung erkannt"]
                steps.append(step)
                return self._finish(False, "failed", "Ablauf wegen wiederholter identischer Entscheidung abgebrochen.", steps, writes)

            decision = response["decision"]
            if decision == "completed":
                if response["machine_state"] != "erreicht":
                    step["status"] = "failed"
                    step["errors"] = ["completed erfordert machine_state=erreicht"]
                    steps.append(step)
                    return self._finish(False, "failed", "Abschluss wurde nicht durch den Maschinenzustand bestaetigt.", steps, writes)
                final_snapshot, final_errors = self._snapshot()
                completion_errors = list(final_errors)
                for check in response["completion_checks"]:
                    state = final_snapshot.get(check["symbol"])
                    if state is None:
                        completion_errors.append(f"Abschlusspruefung unbekanntes Symbol: {check['symbol']}")
                    elif not state.get("valid") or not _same_value(state.get("value"), check["value"]):
                        completion_errors.append(f"Abschlusspruefung nicht erfuellt: {check['symbol']}={check['value']}")
                if completion_errors:
                    step["status"] = "failed"
                    step["errors"] = completion_errors
                    step["snapshot_completion"] = final_snapshot
                    steps.append(step)
                    return self._finish(False, "failed", "Auftrag nicht abgeschlossen: SPS-Zustand bestaetigt das Ziel nicht.", steps, writes)
                step["status"] = "completed"
                step["snapshot_completion"] = final_snapshot
                steps.append(step)
                return self._finish(True, "completed", response["summary"], steps, writes)

            if decision in {"blocked", "fault", "unclear"}:
                step["status"] = decision
                step["errors"] = response["anomalies"] or [response["summary"]]
                steps.append(step)
                return self._finish(False, decision, response["summary"], steps, writes)

            if decision == "wait":
                if response["safe_state_required"]:
                    step["status"] = "safe_state"
                    step["errors"] = ["LLM fordert sicheren Zustand an"]
                    steps.append(step)
                    return self._finish(False, "safe_state", "Sicherer Zustand erforderlich. Keine weitere Aktion ausgefuehrt.", steps, writes)
                step["status"] = "waiting"
                step["wait_seconds"] = response["wait_seconds"]
                steps.append(step)
                self._emit({"event": "wait", "job_id": self.job_id, "step": step_index, "seconds": response["wait_seconds"]})
                self.sleep(float(response["wait_seconds"]))
                snapshot, errors = self._snapshot()
                if errors:
                    return self._finish(False, "failed", "Snapshot nach Wartezustand unvollstaendig.", steps, writes, errors)
                continue

            # continue: refresh the snapshot directly before validating a write.
            current, read_errors = self._snapshot()
            if read_errors:
                step["status"] = "failed"
                step["errors"] = read_errors
                steps.append(step)
                return self._finish(False, "failed", "Snapshot vor Schreibvorgang unvollstaendig.", steps, writes)
            validation_errors = self._validate_action(response, snapshot, current, write_enabled, limits)
            if writes + 1 > int(limits["max_writes_per_job"]):
                validation_errors.append("Maximale Schreibanzahl pro Auftrag ueberschritten")
            if validation_errors:
                step["status"] = "failed"
                step["errors"] = validation_errors
                step["snapshot_at_validation"] = current
                steps.append(step)
                return self._finish(False, "failed", "Pruefung nicht bestanden. Keine Aktion ausgefuehrt.", steps, writes)

            action = response["requested_actions"][0]
            spec = self._spec(action["symbol"])
            ok, error = self.ads.write_value(action["symbol"], spec["data_type"], action["value"])
            self.write_times.append(time.monotonic())
            writes += 1
            write_result: dict[str, Any] = {
                "symbol": action["symbol"],
                "requested": _json_value(action["value"]),
                "write_ok": ok,
                "error": error,
            }
            step["write"] = write_result
            if not ok:
                step["status"] = "failed"
                step["errors"] = ["ADS-Schreibfehler"]
                steps.append(step)
                return self._finish(False, "failed", "ADS-Schreibfehler. Keine weitere Aktion ausgefuehrt.", steps, writes)

            actual, read_ok, read_error = self.ads.read_value(action["symbol"], spec["data_type"])
            write_result.update({"actual": _json_value(actual), "readback_ok": read_ok, "readback_error": read_error})
            if not read_ok or not _same_value(actual, action["value"]):
                step["status"] = "failed"
                step["errors"] = ["Ruecklesen weicht vom Schreibwert ab"]
                steps.append(step)
                return self._finish(False, "failed", "Ruecklesen weicht vom Schreibwert ab.", steps, writes)

            feedback_errors = self._feedback(spec)
            write_result["feedback_errors"] = feedback_errors
            if feedback_errors:
                step["status"] = "failed"
                step["errors"] = feedback_errors
                steps.append(step)
                return self._finish(False, "failed", "Erwartete Sensorreaktion blieb aus.", steps, writes)

            snapshot, read_errors = self._snapshot()
            step["snapshot_after"] = snapshot
            step["status"] = "executed"
            if read_errors:
                step["status"] = "failed"
                step["errors"] = read_errors
                steps.append(step)
                return self._finish(False, "failed", "Snapshot nach Schreibvorgang unvollstaendig.", steps, writes)
            steps.append(step)
            self._emit({
                "event": "step_executed",
                "job_id": self.job_id,
                "step": step_index,
                "decision": response,
                "write": write_result,
                "snapshot_after": snapshot,
            })
            self._emit({
                "event": "next_decision_pending",
                "job_id": self.job_id,
                "step": step_index,
                "message": "Schritt bestaetigt. Naechster realer SPS-Snapshot wird bewertet.",
            })

        return self._finish(False, "step_limit", "Maximale Anzahl von Entscheidungsschritten ueberschritten.", steps, writes)

    def _finish(
        self,
        ok: bool,
        status: str,
        message: str,
        steps: list[dict[str, Any]],
        writes: int,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        result = {
            "ok": ok,
            "status": status,
            "job_id": self.job_id,
            "message": message,
            "step_count": len(steps),
            "write_count": writes,
            "steps": steps,
        }
        if errors:
            result["errors"] = errors
        return result
