from __future__ import annotations

import pytest

from leash.gate import PaymentGate
from leash.policy import PaymentRequest, evaluate


POLICY = {
    "per_transaction_cap_usd": 25,
    "daily_budget_usd": 60,
    "merchant_allowlist": ["allowed@example.com", "other@example.com"],
    "approval_threshold_usd": 10,
    "max_requests_per_minute": 5,
    "approved_task_ids": ["demo-legitimate-task"],
}


def request(
    amount=8,
    email="sandbox-personal-1@example.com",
    task="demo-legitimate-task",
    key="request-1",
):
    return PaymentRequest(key, "agent-1", email, amount, "test purchase", task)


def state(**overrides):
    value = {
        "policy": POLICY,
        "daily_spend_usd": 0,
        "recent_request_count": 0,
        "seen_request_ids": set(),
        "approved_task_ids": {"demo-legitimate-task"},
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("rule", "passing", "failing"),
    [
        ("cap", state(), state()),
        ("daily_budget", state(), state(daily_spend_usd=55)),
        ("allowlist", state(), state()),
        ("approval_threshold", state(), state()),
        ("velocity", state(), state()),
        ("duplicate_request_id", state(), state(seen_request_ids={"request-1"})),
        ("unrequested_task", state(), state(approved_task_ids=set())),
        (
            "payee_mismatch",
            state(catalog={"status": "ok", "price_usd": "8", "merchant_email": "allowed@example.com"}),
            state(catalog={"status": "ok", "price_usd": "8", "merchant_email": "other@example.com"}),
        ),
    ],
)
def test_each_rule_has_pass_and_fail(rule, passing, failing):
    passing_request = request(
        amount=8 if rule != "cap" else 25,
        email="allowed@example.com" if rule != "allowlist" else "allowed@example.com",
    )
    if rule == "approval_threshold":
        passing_request = request(amount=8)
    if rule == "cap":
        failing_request = request(amount=26)
    elif rule == "allowlist":
        failing_request = request(email="blocked@example.com")
    elif rule == "approval_threshold":
        failing_request = request(amount=10)
    elif rule == "velocity":
        failing = state(recent_request_count=5)
        failing_request = request()
    elif rule == "duplicate_request_id":
        failing_request = request()
    elif rule == "unrequested_task":
        failing_request = request()
    else:
        failing_request = request()

    assert rule not in evaluate(passing_request, passing).reasons
    assert rule in evaluate(failing_request, failing).reasons


def test_blocking_injected_request_never_pays(tmp_path):
    class NeverPay:
        def __init__(self):
            self.calls = 0

        def send_payout(self, *args):
            self.calls += 1
            raise AssertionError("blocked request reached PayPal")

        def close(self):
            pass

    payout = NeverPay()
    with PaymentGate(db_path=str(tmp_path / "leash.db"), payout_client=payout) as gate:
        result = gate.process(
            request(amount=200, email="unknown@example.com", task="unapproved")
        )

    assert result["verdict"] == "block"
    assert result["reasons"] == [
        "cap",
        "daily_budget",
        "allowlist",
        "approval_threshold",
        "unrequested_task",
    ]
    assert payout.calls == 0


def test_same_request_id_pays_once(tmp_path):
    class FakePayout:
        def __init__(self):
            self.calls = 0

        def send_payout(self, *args):
            self.calls += 1
            return {"status": "success", "paypal_batch_id": "batch-1"}

        def close(self):
            pass

    payout = FakePayout()
    with PaymentGate(db_path=str(tmp_path / "leash.db"), payout_client=payout) as gate:
        first = gate.process(request(email="sb-fwgx550980251@personal.example.com"))
        second = gate.process(request(email="sb-fwgx550980251@personal.example.com"))

    assert first["status"] == "paid"
    assert second["status"] == "paid"
    assert payout.calls == 1
