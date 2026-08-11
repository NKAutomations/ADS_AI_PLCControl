import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.control.agent_service import AgentControlService


def answer(decision="continue", actions=None, state="in_ausfuehrung", confidence=0.99, wait=False, wait_seconds=0.0):
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "read_only": not bool(actions),
        "machine_state": state,
        "confidence": confidence,
        "observations": ["Mock-Zustand gelesen"],
        "anomalies": [],
        "requested_actions": actions or [],
        "completion_checks": [{"symbol": "FB_A", "value": True}] if decision == "completed" else [],
        "wait": wait,
        "wait_seconds": wait_seconds,
        "safe_state_required": False,
        "summary": "Mock-Entscheidung",
    }


class MockAds:
    connected = True

    def __init__(self):
        self.values = {
            "OUT_A": False,
            "OUT_B": False,
            "FB_A": False,
            "FB_B": False,
            "PERMIT": True,
            "FAULT": False,
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
            raise AssertionError("LLM wurde zu oft gefragt")
        return json.dumps(self.responses.pop(0)), True


def machine():
    return {
        "enabled": True,
        "min_confidence": 0.8,
        "max_response_age_seconds": 15,
        "symbols": [
            {"symbol": "OUT_A", "data_type": "BOOL", "role": "actuator", "writable": True,
             "allowed_values": [False, True], "expected_feedback": [{"symbol": "FB_A", "value": True, "timeout_seconds": 0.01, "poll_interval_seconds": 0.001}]},
            {"symbol": "OUT_B", "data_type": "BOOL", "role": "actuator", "writable": True,
             "allowed_values": [False, True], "expected_feedback": [{"symbol": "FB_B", "value": True, "timeout_seconds": 0.01, "poll_interval_seconds": 0.001}]},
            {"symbol": "FB_A", "data_type": "BOOL", "role": "feedback", "writable": False},
            {"symbol": "FB_B", "data_type": "BOOL", "role": "feedback", "writable": False},
            {"symbol": "PERMIT", "data_type": "BOOL", "role": "permission", "writable": False},
            {"symbol": "FAULT", "data_type": "BOOL", "role": "interlock", "writable": False},
        ],
        "execution": {"required_true": ["PERMIT"], "required_false": ["FAULT"]},
        "agent": {"max_steps": 5, "max_writes_per_job": 3, "max_wait_seconds": 0.05, "max_identical_decisions": 2},
    }


def test_successful_multi_step_job_reads_state_between_steps():
    ads = MockAds()
    llm = MockLlm([
        answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "Schritt A"}]),
        answer(actions=[{"symbol": "OUT_B", "value": True, "reason": "Schritt B"}]),
        answer(decision="completed", state="erreicht"),
    ])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("Grundstellung herstellen", True)
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["write_count"] == 2
    assert len(ads.writes) == 2
    assert len(llm.prompts) == 3
    assert "OUT_A" in llm.prompts[1]
    assert "snapshot_after" in llm.prompts[1]


def test_missing_write_enable_blocks_without_write():
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "test"}])])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("ausfahren", False)
    assert result["ok"] is False
    assert result["write_count"] == 0
    assert ads.writes == []
    assert any("Schreibmodus" in item for item in result["steps"][0]["errors"])


def test_invalid_symbol_is_rejected():
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "NOT_ALLOWED", "value": True, "reason": "test"}])])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("schreiben", True)
    assert result["ok"] is False
    assert ads.writes == []
    assert any("nicht in Maschinenbeschreibung" in item for item in result["steps"][0]["errors"])


def test_write_error_stops_job():
    ads = MockAds()
    ads.fail_write = True
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "test"}])])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("schreiben", True)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["write_count"] == 1
    assert len(llm.responses) == 0


def test_low_confidence_is_rejected():
    ads = MockAds()
    llm = MockLlm([answer(actions=[{"symbol": "OUT_A", "value": True, "reason": "test"}], confidence=0.2)])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("schreiben", True)
    assert result["ok"] is False
    assert ads.writes == []
    assert any("Konfidenz" in item for item in result["steps"][0]["errors"])


def test_repeated_identical_decisions_abort():
    ads = MockAds()
    same = answer(decision="wait", wait=True, wait_seconds=0.001)
    llm = MockLlm([same, same, same])
    result = AgentControlService(ads, llm, machine(), sleep=lambda _: None).execute("warten", True)
    assert result["ok"] is False
    assert "wiederholte identische" in result["message"]
    assert ads.writes == []
