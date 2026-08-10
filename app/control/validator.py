"""Fail-closed validation of the LLM response before any ADS write."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from typing import Any
from .models import ControlResponse, MachineConfig, SymbolSpec

class ValidationFailure(Exception):
    pass

def parse_response(raw: str) -> ControlResponse:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationFailure("LLM-Antwort ist leer")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"LLM-Antwort ist kein gueltiges JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValidationFailure("LLM-Antwort muss ein JSON-Objekt sein")
    try:
        return ControlResponse.model_validate(data)
    except Exception as exc:
        raise ValidationFailure("LLM-Antwort verletzt das verbindliche Schema") from exc

def _same_value(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected

def _type_ok(data_type: str, value: Any) -> bool:
    if data_type == "BOOL":
        return type(value) is bool
    if data_type in {"INT", "DINT", "UINT", "UDINT", "TIME"}:
        return type(value) is int
    if data_type in {"REAL", "LREAL"}:
        return type(value) in {int, float} and not isinstance(value, bool)
    if data_type == "STRING":
        return type(value) is str
    return False

def validate_for_execution(
    response: ControlResponse,
    cfg: MachineConfig,
    snapshot_before: dict[str, dict[str, Any]],
    snapshot_after: dict[str, dict[str, Any]],
    write_enabled: bool,
    recent_write_count: int,
) -> list[str]:
    errors: list[str] = []
    if not cfg.enabled:
        errors.append("Steuerungs-POC ist in der Maschinenbeschreibung deaktiviert")
    if not write_enabled:
        errors.append("Schreibmodus ist in der UI nicht freigegeben")
    if response.read_only:
        errors.append("LLM hat read_only=true gesetzt")
    if response.wait:
        errors.append("LLM fordert Warten an")
    if response.safe_state_required:
        errors.append("LLM fordert sicheren Zustand an")
    if response.confidence < cfg.min_confidence:
        errors.append(f"Konfidenz {response.confidence:.3f} liegt unter {cfg.min_confidence:.3f}")
    timestamp = response.timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        errors.append("Antwortzeitstempel besitzt keine Zeitzone")
    else:
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        if age < -5:
            errors.append("Antwortzeitstempel liegt unzulaessig in der Zukunft")
        elif age > cfg.max_response_age_seconds:
            errors.append("LLM-Antwort ist veraltet")
    if recent_write_count + len(response.requested_actions) > cfg.max_writes_per_minute:
        errors.append("Maximale Schreibfrequenz wuerde ueberschritten")
    by_symbol = {item.symbol: item for item in cfg.symbols}
    for symbol, state in snapshot_before.items():
        after = snapshot_after.get(symbol)
        if after is None or not after.get("valid") or not state.get("valid"):
            errors.append(f"Snapshot ist fuer {symbol} nicht gueltig")
        elif not _same_value(state.get("value"), after.get("value")):
            errors.append(f"Anlagenzustand hat sich seit der Analyse geaendert: {symbol}")
    seen: set[str] = set()
    for action in response.requested_actions:
        if action.symbol in seen:
            errors.append(f"Symbol mehrfach angefordert: {action.symbol}")
        seen.add(action.symbol)
        spec = by_symbol.get(action.symbol)
        if spec is None:
            errors.append(f"Symbol steht nicht auf der Whitelist: {action.symbol}")
            continue
        if spec.role != "actuator" or not spec.writable:
            errors.append(f"Symbol ist nicht als schreibbarer Aktor freigegeben: {action.symbol}")
        if not _type_ok(spec.data_type, action.value):
            errors.append(f"Datentyp passt nicht zu {action.symbol}")
        if spec.allowed_values is not None and not any(_same_value(action.value, allowed) for allowed in spec.allowed_values):
            errors.append(f"Wert ist fuer {action.symbol} nicht erlaubt")
    for symbol in cfg.execution.required_true:
        state = snapshot_after.get(symbol, {})
        if state.get("value") is not True:
            errors.append(f"Erforderliche Freigabe fehlt: {symbol}")
    for symbol in cfg.execution.required_false:
        state = snapshot_after.get(symbol, {})
        if state.get("value") is not False:
            errors.append(f"Verriegelung aktiv oder ungueltig: {symbol}")
    if cfg.execution.mode_symbol and cfg.execution.allowed_modes:
        mode_state = snapshot_after.get(cfg.execution.mode_symbol, {})
        mode_name = cfg.execution.mode_values.get(str(mode_state.get("value")))
        if mode_name not in cfg.execution.allowed_modes:
            errors.append(f"Betriebsart nicht freigegeben: {mode_name}")
    return errors
