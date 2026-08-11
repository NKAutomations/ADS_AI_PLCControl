"""Persistente Prozesssitzung fuer einen Benutzerauftrag.

Kapselt den vollstaendigen Lifecycle-Zustand, das Stopp-Flag und den
Weckzustand. Wird vom AgentControlService erzeugt und vom Server ueber
STATE.active_session nach aussen sichtbar gemacht.

Keine ADS-Zugriffe, keine LLM-Zugriffe, keine Seiteneffekte.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ProcessStatus(str, Enum):
    CREATED       = "created"
    RUNNING       = "running"
    WAITING_TIMER = "waiting_for_timer"
    WAITING_EVENT = "waiting_for_event"
    EXECUTING     = "executing"
    FAULT         = "fault"
    WAITING_ACK   = "waiting_for_ack"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ABORTED       = "aborted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class ProcessSession:
    """Zustand einer einzelnen Prozesssitzung."""

    process_id: str
    command: str

    # Lifecycle
    status: ProcessStatus = ProcessStatus.CREATED
    cycle_count: int = 0
    step_count: int = 0
    total_step_count: int = 0
    total_write_count: int = 0

    # Weckzustand
    waiting_until: str | None = None
    next_wakeup_reason: str | None = None   # "timer" | "event" | "ack"
    wakeup_condition: dict[str, Any] | None = None

    # Stopp-Mechanismus (nicht serialisierbar)
    stop_requested: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    # Protokoll
    last_step_summary: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Zeitstempel
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    def request_stop(self) -> None:
        self.stop_requested.set()

    def is_stop_requested(self) -> bool:
        return self.stop_requested.is_set()

    def public(self) -> dict[str, Any]:
        """JSON-serialisierbares Dict ohne threading.Event."""
        return {
            "process_id":         self.process_id,
            "command":            self.command,
            "status":             self.status.value,
            "cycle_count":        self.cycle_count,
            "step_count":         self.step_count,
            "total_step_count":   self.total_step_count,
            "total_write_count":  self.total_write_count,
            "waiting_until":      self.waiting_until,
            "next_wakeup_reason": self.next_wakeup_reason,
            "wakeup_condition":   self.wakeup_condition,
            "last_step_summary":  self.last_step_summary,
            "steps":              self.steps,
            "errors":             self.errors,
            "created_at":         self.created_at,
            "started_at":         self.started_at,
            "finished_at":        self.finished_at,
            "stop_requested":     self.is_stop_requested(),
        }
