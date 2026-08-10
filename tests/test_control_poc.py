from datetime import datetime, timezone
import json
from app.control.models import MachineConfig
from app.control.validator import ValidationFailure, parse_response, validate_for_execution

CONFIG = {
    "version": "test", "machine_name": "test", "enabled": True,
    "min_confidence": 0.8, "max_response_age_seconds": 15, "max_writes_per_minute": 10,
    "symbols": [
        {"symbol": "OUT", "data_type": "BOOL", "role": "actuator", "description": "out", "writable": True, "allowed_values": [False, True]},
        {"symbol": "PERMIT", "data_type": "BOOL", "role": "permission", "description": "permit", "writable": False},
        {"symbol": "FAULT", "data_type": "BOOL", "role": "interlock", "description": "fault", "writable": False},
    ],
    "execution": {"required_true": ["PERMIT"], "required_false": ["FAULT"]}
}

def response(**changes):
    value = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "read_only": False,
        "machine_state": "bereit", "confidence": 0.95, "observations": [], "anomalies": [],
        "requested_actions": [{"symbol": "OUT", "value": True, "reason": "test"}],
        "wait": False, "safe_state_required": False,
    }
    value.update(changes)
    return value

def snapshot(out=False):
    return {
        "OUT": {"value": out, "valid": True},
        "PERMIT": {"value": True, "valid": True},
        "FAULT": {"value": False, "valid": True},
    }

def test_strict_response_rejects_extra_fields():
    data = response(extra="no")
    try:
        parse_response(json.dumps(data))
    except ValidationFailure:
        return
    assert False, "extra fields must be rejected"

def test_invalid_json_is_rejected():
    try:
        parse_response("not json")
    except ValidationFailure:
        return
    assert False

def test_valid_response_passes():
    cfg = MachineConfig.model_validate(CONFIG)
    parsed = parse_response(json.dumps(response()))
    errors = validate_for_execution(parsed, cfg, snapshot(), snapshot(), True, 0)
    assert errors == []

def test_changed_state_blocks_execution():
    cfg = MachineConfig.model_validate(CONFIG)
    parsed = parse_response(json.dumps(response()))
    changed = snapshot(); changed["FAULT"]["value"] = True
    errors = validate_for_execution(parsed, cfg, snapshot(), changed, True, 0)
    assert any("geaendert" in item or "Verriegelung" in item for item in errors)

def test_missing_ui_write_enable_blocks_execution():
    cfg = MachineConfig.model_validate(CONFIG)
    parsed = parse_response(json.dumps(response()))
    errors = validate_for_execution(parsed, cfg, snapshot(), snapshot(), False, 0)
    assert any("UI" in item for item in errors)
