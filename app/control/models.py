"""Strenge Datenmodelle fuer Zustand, Ereignisse und ereignisgesteuerte Auftraege."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

DataType = Literal["BOOL", "INT", "DINT", "UINT", "UDINT", "REAL", "LREAL", "TIME", "STRING"]
Role = Literal["sensor", "actuator", "feedback", "state", "mode", "permission", "interlock"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class FeedbackSpec(StrictModel):
    symbol: str = Field(min_length=1)
    value: Any
    timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    poll_interval_seconds: float = Field(default=0.1, gt=0, le=5)

class SignalSpec(StrictModel):
    symbol: str = Field(min_length=1)
    data_type: DataType
    role: Role
    description: str = Field(min_length=1)
    writable: bool = False
    safe_value: Any | None = None
    allowed_values: list[Any] | None = None
    llm_trigger: bool = False
    trigger_mode: Literal["none", "change", "edge", "threshold"] = "none"
    minimum_delta: float | None = Field(default=None, ge=0)
    expected_feedback: list[FeedbackSpec] = Field(default_factory=list)

class Condition(StrictModel):
    symbol: str = Field(min_length=1)
    operator: Literal["equals", "not_equals", "greater_than", "less_than", "greater_or_equal", "less_or_equal"] = "equals"
    value: Any

class Relation(StrictModel):
    id: str = Field(min_length=1)
    trigger: Condition
    trigger_event: Literal["level", "rising_edge", "falling_edge", "change"] = "level"
    true_actions: list["PlannedAction"] = Field(default_factory=list)
    false_actions: list["PlannedAction"] = Field(default_factory=list)
    feedback_true: list[FeedbackSpec] = Field(default_factory=list)
    feedback_false: list[FeedbackSpec] = Field(default_factory=list)
    enabled: bool = True

class PlannedAction(StrictModel):
    symbol: str = Field(min_length=1)
    value: Any
    reason: str = Field(min_length=1, max_length=1000)

class ExecutionRules(StrictModel):
    required_true: list[str] = Field(default_factory=list)
    required_false: list[str] = Field(default_factory=list)
    mode_symbol: str | None = None
    allowed_modes: list[str] = Field(default_factory=list)
    mode_values: dict[str, str] = Field(default_factory=dict)

class MachineConfig(StrictModel):
    version: str = Field(min_length=1)
    machine_name: str = Field(min_length=1)
    enabled: bool = False
    min_confidence: float = Field(default=0.85, ge=0, le=1)
    max_writes_per_minute: int = Field(default=10, gt=0, le=1000)
    symbols: list[SignalSpec] = Field(min_length=1)
    relations: list[Relation] = Field(default_factory=list)
    execution: ExecutionRules = Field(default_factory=ExecutionRules)

class SignalValue(StrictModel):
    value: Any
    data_type: DataType
    valid: bool
    timestamp: datetime
    quality: Literal["good", "invalid", "stale"] = "good"

class MachineEvent(StrictModel):
    timestamp: datetime
    event_type: Literal["signal_changed", "rising_edge", "falling_edge", "threshold", "ads_error", "timeout", "write_result", "operator_command"]
    state_version: int = Field(ge=0)
    symbol: str | None = None
    old_value: Any | None = None
    new_value: Any | None = None
    details: dict[str, Any] = Field(default_factory=dict)

class MachineState(StrictModel):
    version: int = Field(default=0, ge=0)
    timestamp: datetime
    values: dict[str, SignalValue] = Field(default_factory=dict)
    recent_events: list[MachineEvent] = Field(default_factory=list, max_length=200)
    active_process_id: str | None = None
    machine_status: Literal["unknown", "ready", "observing", "executing", "waiting_feedback", "fault", "safe_state"] = "unknown"

class ProcessStep(StrictModel):
    id: str = Field(min_length=1)
    when: Condition
    event: Literal["level", "rising_edge", "falling_edge", "change"] = "level"
    actions: list[PlannedAction] = Field(max_length=10)
    feedback: list[FeedbackSpec] = Field(default_factory=list)

class ProcessPlan(StrictModel):
    plan_id: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    description: str = Field(min_length=1)
    steps: list[ProcessStep] = Field(min_length=1, max_length=50)
    max_runtime_seconds: float = Field(default=300, gt=0, le=3600)
    stop_on_error: bool = True

class ProcessSession(StrictModel):
    process_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    plan: ProcessPlan
    status: Literal["created", "waiting_trigger", "validating", "executing", "waiting_feedback", "completed", "failed", "aborted", "safe_state"] = "created"
    current_step_id: str | None = None
    created_at: datetime
    state_version: int = Field(ge=0)
    completed_steps: list[str] = Field(default_factory=list)

# Kompatibilitaetsmodelle fuer den bestehenden One-shot-Service.
class RequestedAction(StrictModel):
    symbol: str = Field(min_length=1)
    value: Any
    reason: str = Field(min_length=1, max_length=1000)

class ControlResponse(StrictModel):
    timestamp: datetime
    read_only: bool
    machine_state: Literal["unbekannt", "bereit", "in_ausfuehrung", "erreicht", "stoerung", "pruefen"]
    confidence: float = Field(ge=0, le=1)
    observations: list[str] = Field(max_length=30)
    anomalies: list[str] = Field(max_length=30)
    requested_actions: list[RequestedAction] = Field(max_length=10)
    wait: bool
    safe_state_required: bool

# Alter Name bleibt fuer bestehende Importstellen erhalten.
SymbolSpec = SignalSpec
