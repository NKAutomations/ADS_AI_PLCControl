"""Ergaenzende Tests fuer AgentControlService (Stopp, Ack, Rueckwaertskompatibilitaet).

Diese Datei ergaenzt test_agent_service.py um die neuen Faehigkeiten.
Die bestehenden Tests in test_agent_service.py bleiben unveraendert.
"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.control.agent_service import AgentControlService
from app.control.process_session import ProcessSession


def _ts():
    return datetime.now(timezone.utc).isoformat()


def answer(decision="continue", actions=None, state="in_ausfuehrung",
           confidence=0.99, wait=False, wait_seconds=0.0):
    return {
        "timestamp":         _ts(),
        "decision":          decision,
        "read_only":         not bool(actions),
        "machine_state":     state,
        "confidence":        confidence,
        "observations":      ["Mock"],
        "anomalies":         [],
        "requested_actions": actions or [],
        "completion_checks": [{"symbol": "FAULT", "value": False}]
                             if decision == "completed" else [],
        "wait":              wait,
        "wait_seconds":      wait_seconds,
        "safe_state_required": False,
        "summary":           "Mock",
    }


class MockAds:
    connected = True
    def __init__(self):
        self.values = {
            "OUT_A": False, "OUT_B": False,
            "FB_A": False, "FB_B": False,
            "PERMIT": True, "FAULT": False, "FAULT_ACK": False,
        }
        self.writes = []
    def read_value(self, s, t):
        return self.values.get(s, None), s in self.values, ""
    def write_value(self, s, t, v):
        self.writes.append((s, v))
        self.values[s] = v
        if s == "OUT_A" and v: self.values["FB_A"] = True
        if s == "OUT_B" and v: self.values["FB_B"] = True
        return True, ""


class MockLlm:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
    def ask(self, p):
        self.prompts.append(p)
        if not self.responses:
            raise AssertionError("LLM zu oft gefragt")
        return json.dumps(self.responses.pop(0)), True


def machine_cfg(**overrides):
    cfg = {
        "enabled": True,
        "min_confidence": 0.8,
        "max_response_age_seconds": 15,
        "symbols": [
            {"symbol": "OUT_A", "data_type": "BOOL", "role": "actuator",
             "writable": True, "allowed_values": [False, True],
             "expected_feedback": [{"symbol": "FB_A", "value": True,
                                    "timeout_seconds": 0.01, "poll_interval_seconds": 0.001}]},
            {"symbol": "OUT_B", "data_type": "BOOL", "role": "actuator",
             "writable": True, "allowed_values": [False, True],
             "expected_feedback": [{"symbol": "FB_B", "value": True,
                                    "timeout_seconds": 0.01, "poll_interval_seconds": 0.001}]},
            {"symbol": "FB_A",     "data_type": "BOOL", "role": "feedback",     "writable": False},
            {"symbol": "FB_B",     "data_type": "BOOL", "role": "feedback",     "writable": False},
            {"symbol": "PERMIT",   "data_type": "BOOL", "role": "permission",   "writable": False},
            {"symbol": "FAULT",    "data_type": "BOOL", "role": "fault_signal", "writable": False},
            {"symbol": "FAULT_ACK","data_type": "BOOL", "role": "fault_ack",    "writable": False},
        ],
        "execution": {"required_true": ["PERMIT"], "required_false": []},
        "agent": {
            "max_steps": 5, "max_writes_per_job": 10,
            "max_writes_per_minute": 100,
            "job_timeout_seconds": 30.0,
            "max_wait_seconds": 0.1,
            "max_identical_decisions": 2,
            "loop_mode": False, "max_cycles": 0,
            "cycle_timeout_seconds": 30.0,
            "ack_timeout_seconds": 0.05,
            "ack_poll_interval": 0.005,
        },
    }
    cfg["agent"].update(overrides)
    return cfg


def svc(ads, llm, **kw):
    return AgentControlService(ads, llm, machine_cfg(**kw), sleep=lambda _: None)


# ── Rueckwaertskompatibilitaet ────────────────────────────────────────────

def test_backward_compat_single_step():
    """loop_mode=false -> Verhalten identisch zu vorher."""
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht"),
    ])
    r = svc(ads, llm).execute("test", True)
    assert r["ok"] is True
    assert r["cycle_count"] == 1
    assert r["write_count"] == 1


def test_result_contains_session():
    """Ergebnis enthaelt session-Dict."""
    ads = MockAds()
    llm = MockLlm([answer(decision="completed", state="erreicht",
                          actions=None)])
    # completed ohne vorherige Aktion -> completion_checks noetig
    llm2 = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht"),
    ])
    r = svc(ads, llm2).execute("test", True)
    assert "session" in r
    assert r["session"]["status"] == "completed"


# ── Stopp-Signal ──────────────────────────────────────────────────────────

def test_stop_flag_blocks_before_cycle():
    """Stopp vor dem ersten Zyklus -> sofortiger Abbruch."""
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    session = ProcessSession(process_id="s", command="x")
    session.request_stop()
    r = svc(ads, llm).execute("x", True, session=session)
    assert r["ok"] is False
    assert r["status"] == "aborted"
    assert ads.writes == []


def test_stop_flag_blocks_write():
    """Stopp-Flag gesetzt -> Schreibvalidierung schlaegt fehl."""
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    session = ProcessSession(process_id="s2", command="x")

    # Stopp setzen nachdem LLM geantwortet hat, aber vor Schreiben
    original_validate = AgentControlService._validate_action
    call_count = [0]
    def patched_validate(self_inner, *args, **kwargs):
        call_count[0] += 1
        session.request_stop()
        return original_validate(self_inner, *args, **kwargs)

    AgentControlService._validate_action = patched_validate
    try:
        r = svc(ads, llm).execute("x", True, session=session)
    finally:
        AgentControlService._validate_action = original_validate

    assert r["ok"] is False
    assert ads.writes == []


# ── Fehlerquittierung ─────────────────────────────────────────────────────

def test_fault_decision_triggers_ack_wait():
    """KI meldet fault -> Service wartet auf fault_ack-Symbol."""
    ads = MockAds()

    # Quittierung nach kurzer Zeit
    def ack_later():
        import time; time.sleep(0.02)
        ads.values["FAULT_ACK"] = True

    t = threading.Thread(target=ack_later, daemon=True)
    t.start()

    llm = MockLlm([
        {"timestamp": _ts(), "decision": "fault", "read_only": True,
         "machine_state": "stoerung", "confidence": 0.99,
         "observations": [], "anomalies": ["Stoerung"],
         "requested_actions": [], "completion_checks": [],
         "wait": False, "wait_seconds": 0.0,
         "safe_state_required": False, "summary": "Stoerung"},
        answer(decision="completed", state="erreicht"),
    ])

    service = AgentControlService(
        ads, llm, machine_cfg(ack_timeout_seconds=0.5, ack_poll_interval=0.005),
        sleep=lambda s: __import__('time').sleep(s),
    )
    r = service.execute("test", True)
    t.join(timeout=1)

    assert r["ok"] is True
    assert r["status"] == "completed"
    assert r["cycle_count"] == 2


def test_fault_ack_not_written_by_app():
    """Die Anwendung schreibt das fault_ack-Symbol NICHT selbst."""
    ads = MockAds()
    llm = MockLlm([
        {"timestamp": _ts(), "decision": "fault", "read_only": True,
         "machine_state": "stoerung", "confidence": 0.99,
         "observations": [], "anomalies": [],
         "requested_actions": [], "completion_checks": [],
         "wait": False, "wait_seconds": 0.0,
         "safe_state_required": False, "summary": "Stoerung"},
    ])
    # Kein Ack kommt -> timeout
    r = svc(ads, llm, ack_timeout_seconds=0.01).execute("test", True)
    assert r["ok"] is False
    # FAULT_ACK darf nicht in den Schreibvorgaengen auftauchen
    written_symbols = [w[0] for w in ads.writes]
    assert "FAULT_ACK" not in written_symbols


# ── Prozessstatus in API-Antwort ──────────────────────────────────────────

def test_session_status_in_result():
    """Ergebnis-Dict enthaelt korrekten Prozessstatus."""
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht"),
    ])
    r = svc(ads, llm).execute("test", True)
    assert r["session"]["status"] == "completed"
    assert r["session"]["total_write_count"] == 1


def test_session_aborted_status():
    """Abgebrochener Auftrag hat status=aborted in der Sitzung."""
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    session = ProcessSession(process_id="ab", command="x")
    session.request_stop()
    r = svc(ads, llm).execute("x", True, session=session)
    assert r["session"]["status"] == "aborted"


# ── Ausfuehren ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} bestanden, {failed} fehlgeschlagen")
    if failed:
        sys.exit(1)
