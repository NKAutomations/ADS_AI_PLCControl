"""Tests fuer Dauerschleife, Fehlerquittierung, Stopp und Limits.

Alle Tests laufen ohne echte ADS-Schreibzugriffe (Mock-ADS, Mock-LLM).
"""
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.control.agent_service import AgentControlService
from app.control.process_session import ProcessSession, ProcessStatus


# ═══════════════════════════════════════════════════════════════════════════
# Mock-Infrastruktur
# ═══════════════════════════════════════════════════════════════════════════

def _ts():
    return datetime.now(timezone.utc).isoformat()


def answer(
    decision="continue",
    actions=None,
    state="in_ausfuehrung",
    confidence=0.99,
    wait=False,
    wait_seconds=0.0,
    fault=False,
    completion_checks=None,
):
    if fault:
        decision = "fault"
        state = "stoerung"
        actions = None
    if completion_checks is None:
        completion_checks = [{"symbol": "FB_A", "value": True}] if decision == "completed" else []
    return {
        "timestamp":         _ts(),
        "decision":          decision,
        "read_only":         not bool(actions),
        "machine_state":     state,
        "confidence":        confidence,
        "observations":      ["Mock-Zustand gelesen"],
        "anomalies":         ["Stoerung aktiv"] if fault else [],
        "requested_actions": actions or [],
        "completion_checks": completion_checks,
        "wait":              wait,
        "wait_seconds":      wait_seconds,
        "safe_state_required": False,
        "summary":           "Mock-Entscheidung",
    }


class MockAds:
    connected = True

    def __init__(self):
        self.values = {
            "OUT_A":    False,
            "OUT_B":    False,
            "FB_A":     False,
            "FB_B":     False,
            "PERMIT":   True,
            "FAULT":    False,
            "FAULT_ACK": False,
        }
        self.writes = []
        self.fail_write = False
        self.fail_readback = False

    def read_value(self, symbol, data_type):
        if symbol not in self.values:
            return None, False, "unbekanntes Mock-Symbol"
        value = self.values[symbol]
        if self.fail_readback and symbol == "OUT_A" and self.writes:
            value = False
        return value, True, ""

    def write_value(self, symbol, data_type, value):
        self.writes.append((symbol, value))
        if self.fail_write:
            return False, "Mock-Schreibfehler"
        self.values[symbol] = value
        if symbol == "OUT_A" and value is True:
            self.values["FB_A"] = True
        if symbol == "OUT_B" and value is True:
            self.values["FB_B"] = True
        return True, ""


class MockLlm:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError(f"LLM wurde zu oft gefragt ({len(self.prompts)}x)")
        return json.dumps(self.responses.pop(0)), True


def machine(loop_mode=False, max_cycles=0, max_steps=10,
            max_writes=20, ack_timeout=0.1):
    return {
        "enabled": True,
        "min_confidence": 0.8,
        "max_response_age_seconds": 15,
        "symbols": [
            {
                "symbol": "OUT_A", "data_type": "BOOL",
                "role": "actuator", "writable": True,
                "allowed_values": [False, True],
                "expected_feedback": [{
                    "symbol": "FB_A", "value": True,
                    "timeout_seconds": 0.01, "poll_interval_seconds": 0.001,
                }],
            },
            {
                "symbol": "OUT_B", "data_type": "BOOL",
                "role": "actuator", "writable": True,
                "allowed_values": [False, True],
                "expected_feedback": [{
                    "symbol": "FB_B", "value": True,
                    "timeout_seconds": 0.01, "poll_interval_seconds": 0.001,
                }],
            },
            {"symbol": "FB_A",     "data_type": "BOOL", "role": "feedback",     "writable": False},
            {"symbol": "FB_B",     "data_type": "BOOL", "role": "feedback",     "writable": False},
            {"symbol": "PERMIT",   "data_type": "BOOL", "role": "permission",   "writable": False},
            {"symbol": "FAULT",    "data_type": "BOOL", "role": "fault_signal", "writable": False},
            {"symbol": "FAULT_ACK","data_type": "BOOL", "role": "fault_ack",    "writable": False},
        ],
        "execution": {
            "required_true":  ["PERMIT"],
            "required_false": [],
        },
        "agent": {
            "max_steps":              max_steps,
            "max_writes_per_job":     max_writes,
            "max_writes_per_minute":  100,
            "job_timeout_seconds":    30.0,
            "max_wait_seconds":       0.1,
            "max_identical_decisions": 2,
            "loop_mode":              loop_mode,
            "max_cycles":             max_cycles,
            "cycle_timeout_seconds":  30.0,
            "ack_timeout_seconds":    ack_timeout,
            "ack_poll_interval":      0.005,
        },
    }


def make_service(ads, llm, cfg, use_real_sleep=False):
    sleep_fn = time.sleep if use_real_sleep else (lambda _: None)
    return AgentControlService(ads, llm, cfg, sleep=sleep_fn)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Einzelauftrag (loop_mode=false, Rueckwaertskompatibilitaet)
# ═══════════════════════════════════════════════════════════════════════════

def test_single_job_completed():
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "Schritt A"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
    ])
    r = make_service(ads, llm, machine()).execute("Grundstellung", True)
    assert r["ok"] is True, r["message"]
    assert r["status"] == "completed"
    assert r["write_count"] == 1
    assert r["cycle_count"] == 1


def test_single_job_missing_write_enable():
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    r = make_service(ads, llm, machine()).execute("test", False)
    assert r["ok"] is False
    assert r["write_count"] == 0
    assert ads.writes == []


def test_single_job_invalid_symbol():
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "UNKNOWN", "value": True, "reason": "x"}])])
    r = make_service(ads, llm, machine()).execute("test", True)
    assert r["ok"] is False
    assert ads.writes == []


def test_single_job_write_error():
    ads = MockAds()
    ads.fail_write = True
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    r = make_service(ads, llm, machine()).execute("test", True)
    assert r["ok"] is False
    assert r["status"] == "failed"


def test_single_job_readback_mismatch():
    ads = MockAds()
    ads.fail_readback = True
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    r = make_service(ads, llm, machine()).execute("test", True)
    assert r["ok"] is False
    assert "Ruecklesen" in r["message"]


def test_single_job_low_confidence():
    ads = MockAds()
    llm = MockLlm([answer(
        actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}],
        confidence=0.2,
    )])
    r = make_service(ads, llm, machine()).execute("test", True)
    assert r["ok"] is False
    assert ads.writes == []


def test_single_job_repeated_decisions():
    """Wiederholte identische Entscheidung -> Abbruch."""
    ads = MockAds()
    same = answer(decision="wait", wait=True, wait_seconds=0.001)
    llm = MockLlm([same, same, same])
    r = make_service(ads, llm, machine()).execute("warten", True)
    assert r["ok"] is False
    # Nachricht enthaelt "wiederholter identischer" oder "identische"
    assert "identisch" in r["message"].lower() or "identische" in r["message"].lower()


def test_single_job_invalid_json():
    class BadLlm:
        def ask(self, p): return "kein json", True
    r = make_service(MockAds(), BadLlm(), machine()).execute("test", True)
    assert r["ok"] is False


def test_single_job_fault_no_ack_configured():
    ads = MockAds()
    cfg = machine()
    cfg["symbols"] = [s for s in cfg["symbols"] if s["role"] != "fault_ack"]
    llm = MockLlm([answer(fault=True)])
    r = make_service(ads, llm, cfg).execute("test", True)
    assert r["ok"] is False
    assert r["status"] == "failed"
    assert ads.writes == []


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Dauerschleife (loop_mode=true)
# ═══════════════════════════════════════════════════════════════════════════

def test_loop_mode_two_cycles_max_cycles():
    """max_cycles=2 -> nach zwei Zyklen Abbruch mit 'Zyklusanzahl'."""
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "Z1"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
        answer(actions=[{"symbol": "OUT_B", "value": True, "reason": "Z2"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_B", "value": True}]),
    ])
    cfg = machine(loop_mode=True, max_cycles=2)
    r = make_service(ads, llm, cfg).execute("loop", True)
    assert r["ok"] is False
    assert r["status"] == "aborted"
    assert r["cycle_count"] == 2
    assert "Zyklusanzahl" in r["message"]


def test_loop_mode_stop_before_write():
    """Stopp-Flag gesetzt -> kein Schreibvorgang."""
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    cfg = machine(loop_mode=True)
    session = ProcessSession(process_id="pre-write-stop", command="x")
    session.request_stop()
    r = make_service(ads, llm, cfg).execute("x", True, session=session)
    assert r["ok"] is False
    assert r["status"] == "aborted"
    assert ads.writes == []


def test_loop_mode_stop_via_event():
    """Stopp-Flag wird von aussen gesetzt -> sauberer Abbruch."""
    ads = MockAds()
    # Genug Antworten fuer mehrere Zyklen
    llm = MockLlm([
        answer(decision="wait", wait=True, wait_seconds=0.05),
        answer(decision="wait", wait=True, wait_seconds=0.05),
        answer(decision="wait", wait=True, wait_seconds=0.05),
        answer(decision="wait", wait=True, wait_seconds=0.05),
    ])
    cfg = machine(loop_mode=True)
    cfg["agent"]["max_wait_seconds"] = 1.0

    session = ProcessSession(process_id="stop-test", command="stop-test")

    def set_stop():
        time.sleep(0.03)
        session.request_stop()

    t = threading.Thread(target=set_stop, daemon=True)
    t.start()

    service = AgentControlService(ads, llm, cfg, sleep=time.sleep)
    r = service.execute("stop-test", True, session=session)
    t.join(timeout=1)

    assert r["ok"] is False
    assert r["status"] == "aborted"
    assert ads.writes == []


def test_loop_mode_write_enable_revoked():
    """Schreibfreigabe wird nach erstem Schreibvorgang widerrufen."""
    ads = MockAds()
    # Zyklus 1: Schritt 1 schreibt, dann completed
    # Zyklus 2: Schreibfreigabe fehlt -> Abbruch
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
        answer(actions=[{"symbol": "OUT_B", "value": True, "reason": "y"}]),
        # Fallback falls noch ein Schritt kommt
        answer(decision="unclear", state="pruefen"),
    ])
    cfg = machine(loop_mode=True, max_cycles=0)

    wrote = [False]
    def write_enabled():
        # Freigabe nur bis zum ersten Schreibvorgang
        if wrote[0]:
            return False
        return True

    original_write = ads.write_value
    def tracked_write(s, t, v):
        wrote[0] = True
        return original_write(s, t, v)
    ads.write_value = tracked_write

    r = make_service(ads, llm, cfg).execute("x", write_enabled)
    assert r["ok"] is False
    # Mindestens ein Schreibvorgang im ersten Zyklus
    assert r["write_count"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Fehlerquittierung
# ═══════════════════════════════════════════════════════════════════════════

def test_fault_then_ack_then_new_cycle():
    """Fehlermerker aktiv -> waiting_for_ack -> Quittierung -> neuer Zyklus."""
    ads = MockAds()
    ads.values["FAULT"] = True

    fault_ans = answer(fault=True)
    # Zyklus 2: FAULT ist jetzt False, completed
    completed_ans = answer(
        decision="completed", state="erreicht",
        completion_checks=[{"symbol": "FAULT", "value": False}],
    )
    llm = MockLlm([fault_ans, completed_ans])
    cfg = machine(loop_mode=False, ack_timeout=0.5)

    def set_ack():
        time.sleep(0.05)
        ads.values["FAULT_ACK"] = True
        ads.values["FAULT"] = False

    t = threading.Thread(target=set_ack, daemon=True)
    t.start()

    r = make_service(ads, llm, cfg, use_real_sleep=True).execute("test", True)
    t.join(timeout=1)

    assert r["ok"] is True, r["message"]
    assert r["status"] == "completed"
    assert r["cycle_count"] == 2


def test_fault_ack_timeout():
    """Quittierung kommt nicht -> failed."""
    ads = MockAds()
    llm = MockLlm([answer(fault=True)])
    cfg = machine(ack_timeout=0.02)
    r = make_service(ads, llm, cfg, use_real_sleep=True).execute("test", True)
    assert r["ok"] is False
    assert r["status"] == "failed"
    assert "quittierung" in r["message"].lower()


def test_fault_ack_stop_during_wait():
    """Stopp waehrend Quittierungswartezeit."""
    ads = MockAds()
    llm = MockLlm([answer(fault=True)])
    cfg = machine(ack_timeout=5.0)
    session = ProcessSession(process_id="ack-stop", command="x")

    def stop_after():
        time.sleep(0.02)
        session.request_stop()

    t = threading.Thread(target=stop_after, daemon=True)
    t.start()

    service = AgentControlService(ads, llm, cfg, sleep=time.sleep)
    r = service.execute("x", True, session=session)
    t.join(timeout=1)

    assert r["ok"] is False
    assert r["status"] == "aborted"


def test_fault_no_ack_symbol():
    """Kein fault_ack-Symbol konfiguriert -> sofortiger Abbruch."""
    ads = MockAds()
    cfg = machine()
    cfg["symbols"] = [s for s in cfg["symbols"] if s["role"] != "fault_ack"]
    llm = MockLlm([answer(fault=True)])
    r = make_service(ads, llm, cfg).execute("test", True)
    assert r["ok"] is False
    assert r["status"] == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Limits
# ═══════════════════════════════════════════════════════════════════════════

def test_max_cycles_limit():
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
    ])
    cfg = machine(loop_mode=True, max_cycles=1)
    r = make_service(ads, llm, cfg).execute("x", True)
    assert r["ok"] is False
    assert r["status"] == "aborted"
    assert r["cycle_count"] == 1
    assert "Zyklusanzahl" in r["message"]


def test_max_writes_per_job():
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(actions=[{"symbol": "OUT_B", "value": True, "reason": "y"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
    ])
    cfg = machine(max_writes=1)
    r = make_service(ads, llm, cfg).execute("x", True)
    assert r["ok"] is False
    assert r["write_count"] <= 1


def test_step_limit():
    ads = MockAds()
    llm = MockLlm([
        answer(decision="wait", wait=True, wait_seconds=0.001),
        answer(decision="wait", wait=True, wait_seconds=0.001),
        answer(decision="wait", wait=True, wait_seconds=0.001),
    ])
    cfg = machine(max_steps=2)
    r = make_service(ads, llm, cfg).execute("x", True)
    assert r["ok"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Tests: Prozesssitzung
# ═══════════════════════════════════════════════════════════════════════════

def test_process_session_public_serializable():
    s = ProcessSession(process_id="abc", command="test")
    d = s.public()
    assert d["process_id"] == "abc"
    assert d["status"] == "created"
    assert isinstance(d["stop_requested"], bool)
    json.dumps(d)  # muss ohne Fehler serialisierbar sein


def test_process_session_stop_flag():
    s = ProcessSession(process_id="x", command="y")
    assert not s.is_stop_requested()
    s.request_stop()
    assert s.is_stop_requested()
    assert s.public()["stop_requested"] is True


def test_new_command_after_abort():
    """Nach Abbruch kann ein neuer Auftrag gestartet werden."""
    ads = MockAds()
    session1 = ProcessSession(process_id="j1", command="x")
    session1.request_stop()
    llm1 = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}])])
    r1 = make_service(ads, llm1, machine()).execute("x", True, session=session1)
    assert r1["status"] == "aborted"

    ads2 = MockAds()
    llm2 = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "x"}]),
        answer(decision="completed", state="erreicht",
               completion_checks=[{"symbol": "FB_A", "value": True}]),
    ])
    r2 = make_service(ads2, llm2, machine()).execute("Grundstellung", True)
    assert r2["ok"] is True
    assert r2["status"] == "completed"


# ═══════════════════════════════════════════════════════════════════════════
# Ausfuehren
# ═══════════════════════════════════════════════════════════════════════════

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
