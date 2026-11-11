"""The event bus reports decisions and can never change or delay one."""

from __future__ import annotations

import json
import threading
import time

import pytest

from leash import events
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


SELLER = "sb-fwgx550980251@personal.example.com"


class FakePayout:
    def __init__(self, status="success"):
        self.status = status
        self.calls = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": self.status, "paypal_batch_id": "batch-1"}

    def get_payout_status(self, batch_id):
        return {"batch_status": "PENDING", "paypal_batch_id": batch_id}

    def close(self):
        pass


def make_gate(tmp_path, on_event=None, payout=None, name="leash.db"):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 25,
        "daily_budget_usd": 60,
        "merchant_allowlist": [SELLER],
        "approval_threshold_usd": 10,
        "max_requests_per_minute": 100,
        "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
    }))
    return PaymentGate(
        db_path=str(tmp_path / name),
        policy_path=policy,
        payout_client=payout or FakePayout(),
        risk_signal=lambda request, task: ("none", "none"),
        on_event=on_event,
    )


def req(key, amount="8.00", email=SELLER):
    return PaymentRequest(key, "agent-1", email, amount, "gift", "demo-legitimate-task")


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, record):
        self.events.append((kind, record["request_id"], record["status"]))

    def kinds(self, request_id=None):
        return [k for k, rid, _ in self.events if request_id in (None, rid)]


# --- each verdict path emits its event exactly once ---------------------------

def test_allowed_payment_emits_created_then_paid(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("a-1"))

    assert recorder.kinds() == ["decision.created", "decision.paid"]


def test_held_payment_emits_held_once(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("h-1", amount="15.00"))

    assert recorder.kinds() == ["decision.held"]


def test_blocked_payment_emits_blocked_once(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("b-1", amount="500.00", email="attacker@example.com"))

    assert recorder.kinds() == ["decision.blocked"]


def test_replayed_request_id_emits_nothing_new(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("a-1"))
    gate.process(req("a-1"))

    assert recorder.kinds() == ["decision.created", "decision.paid"]


def test_approve_emits_approved_then_paid(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("h-1", amount="15.00"))
    gate.approve("h-1", "human")

    assert recorder.kinds() == ["decision.held", "decision.approved", "decision.paid"]


def test_deny_emits_denied_once(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder)
    gate.process(req("h-1", amount="15.00"))
    gate.deny("h-1", "human", "no")
    gate.deny("h-1", "human", "no again")  # already resolved: no second event

    assert recorder.kinds() == ["decision.held", "decision.denied"]


def test_failed_payout_emits_failed(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder, payout=FakePayout(status="failed"))
    gate.process(req("a-1"))

    assert recorder.kinds() == ["decision.created", "decision.failed"]


def test_polling_a_still_submitted_payout_does_not_repeat_the_event(tmp_path):
    recorder = Recorder()
    gate = make_gate(tmp_path, recorder, payout=FakePayout(status="submitted"))
    gate.process(req("a-1"))
    gate.poll_submitted()
    gate.poll_submitted()

    assert recorder.kinds() == ["decision.created", "decision.submitted"]


# --- sinks cannot change or delay a decision ---------------------------------

def strip_volatile(record):
    return {
        key: value for key, value in record.items()
        if key not in {"request_id", "created_at", "expires_at", "request", "request_json"}
    }


def test_a_raising_on_event_hook_does_not_change_process_output(tmp_path):
    def broken(kind, record):
        raise RuntimeError("sink down")

    quiet = make_gate(tmp_path, lambda *_: None, name="a.db")
    loud = make_gate(tmp_path, broken, name="b.db")
    for amount in ("8.00", "15.00", "500.00"):
        expected = quiet.process(req(f"q-{amount}", amount=amount))
        actual = loud.process(req(f"q-{amount}", amount=amount))
        assert strip_volatile(actual) == strip_volatile(expected)


def test_a_raising_registered_sink_does_not_change_process_output(tmp_path, caplog):
    def broken(kind, payload):
        raise RuntimeError("sink down")

    events.register(broken)
    gate = make_gate(tmp_path)  # default hook: the real event bus
    record = gate.process(req("a-1"))
    events.drain()

    assert record["status"] == "paid"
    assert "Event sink broken failed" in caplog.text


def test_a_slow_sink_does_not_delay_the_decision(tmp_path):
    release = threading.Event()
    seen = []

    def slow(kind, payload):
        release.wait(2)
        seen.append(kind)

    events.register(slow)
    gate = make_gate(tmp_path)
    started = time.monotonic()
    record = gate.process(req("h-1", amount="15.00"))
    elapsed = time.monotonic() - started
    release.set()
    events.drain()

    assert record["status"] == "pending"
    assert elapsed < 0.5
    assert seen == ["decision.held"]


def test_a_sink_cannot_change_what_another_sink_sees(tmp_path):
    seen = []

    def vandal(kind, payload):
        payload["verdict"] = "allow"
        payload["status"] = "paid"

    def watcher(kind, payload):
        seen.append(payload["verdict"])

    events.register(vandal)
    events.register(watcher)
    gate = make_gate(tmp_path)
    gate.process(req("b-1", amount="500.00", email="attacker@example.com"))
    events.drain()

    assert seen == ["block"]
    assert gate.get("b-1")["status"] == "blocked"


def test_payload_is_the_public_shape_plus_event_kind(tmp_path):
    payloads = []
    events.register(lambda kind, payload: payloads.append(payload))
    gate = make_gate(tmp_path)
    gate.process(req("h-1", amount="15.00"))
    events.drain()

    [payload] = payloads
    assert payload["event"] == "decision.held"
    assert payload["amount_usd"] == "15.00"
    assert payload["verdict"] == "hold_for_human"
    assert "policy_snapshot" not in payload  # internal fields stay internal


def test_unknown_event_kind_is_dropped():
    seen = []
    events.register(lambda kind, payload: seen.append(kind))
    events.emit("decision.whatever", {"request_id": "x"})
    events.drain()

    assert seen == []


def test_install_from_env_is_a_no_op_without_env(monkeypatch):
    for name in ("ZAPIER_HOOK_URL", "ELASTIC_CLOUD_ID", "ELASTIC_URL", "ELASTIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert events.install_from_env() == []
    assert events.install_from_env() == []  # second call does nothing
    assert events.sinks() == []
