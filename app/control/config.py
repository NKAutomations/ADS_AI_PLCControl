"""Load and validate the versioned machine description."""
from __future__ import annotations
import json
from pathlib import Path
from .models import MachineConfig

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "control_config.json"

def load_machine_config(path: str | Path | None = None) -> MachineConfig:
    target = Path(path) if path else DEFAULT_PATH
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    cfg = MachineConfig.model_validate(data)
    symbols = [item.symbol for item in cfg.symbols]
    if len(symbols) != len(set(symbols)):
        raise ValueError("Maschinenbeschreibung enthaelt doppelte Symbole")
    execution = cfg.execution
    for item in execution.required_true + execution.required_false:
        if item not in symbols:
            raise ValueError(f"Freigabe-/Verriegelungssymbol fehlt: {item}")
    if execution.mode_symbol and execution.mode_symbol not in symbols:
        raise ValueError("mode_symbol ist nicht in symbols definiert")
    for item in cfg.symbols:
        for feedback in item.expected_feedback:
            if feedback.symbol not in symbols:
                raise ValueError(f"Feedbacksymbol fehlt: {feedback.symbol}")
    return cfg
