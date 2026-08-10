from __future__ import annotations
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "config" / "app_config.json"
MACHINE_PATH = ROOT / "config" / "machine_config.json"

def load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        save(path, default)
        return default
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} muss ein JSON-Objekt enthalten")
    return data

def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temporary.replace(path)

def app_config() -> dict[str, Any]:
    return load(APP_PATH, {"server": {"host": "127.0.0.1", "port": 8080}, "ads": {}, "llm": {}})

def machine_config() -> dict[str, Any]:
    return load(MACHINE_PATH, {"version": "0.1.0-poc", "machine_name": "Maschine", "enabled": False, "min_confidence": 0.85, "max_response_age_seconds": 15, "max_writes_per_minute": 10, "symbols": [], "execution": {"required_true": [], "required_false": [], "mode_symbol": None, "allowed_modes": [], "mode_values": {}}})
