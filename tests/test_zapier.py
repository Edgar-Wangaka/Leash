"""Zapier gets held and blocked decisions as flat fields, never a token."""

from __future__ import annotations

import json

import httpx
import pytest

from leash import events, sinks
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


SELLER = "sb-fwgx550980251@personal.example.com"
HOOK = "https://hooks.zapier.com/hooks/catch/123/abc/"


class FakePayout:
    def send_payout(self, *args):
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


def capture():
    sent = []

    def handler(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"status": "success"})

    return sent, httpx.Client(transport=httpx.MockTransport(handler))


def make_gate(tmp_path, catalog=None):
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
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy,
        payout_client=FakePayout(),
        risk_signal=lambda request, task: ("none", "none"),
        price_lookup=(lambda product_id: catalog) if catalog else None,
    )


def req(key, amount="8.00", email=SELLER, product_id=None):
    return PaymentRequest(
        key, "agent-1", email, amount, "Birthday Bear", "demo-legitimate-task",
        product_id=product_id,
    )


@pytest.fixture
def zapier(monkeypatch):
    monkeypatch.setenv("LEASH_PUBLIC_HOST", "leash.example.com")
    monkeypatch.setenv("LEASH_DASHBOARD_TOKEN", "secret-dashboard-token-123")
    sent, client = capture()
    sink = sinks.ZapierSink(
        HOOK, kinds=sinks.ZapierSink.DEFAULT_KINDS, http_client=client,
        dashboard=sinks.dashboard_url(),
    )
    events.register(sink)
    return sent


def test_held_payment_reaches_zapier_with_flat_fields(tmp_path, zapier):
    make_gate(tmp_path).process(req("h-1", amount="15.00"))
    events.drain()

    [(url, body)] = zapier
    assert url == HOOK
    assert body["event"] == "decision.held"
    assert body["amount_usd"] == "15.00"
    assert body["seller"] == SELLER
    assert body["agent"] == "agent-1"
    assert body["item"] == "Birthday Bear"
    assert body["reasons"] == "approval_threshold"
    assert body["is_attack"] is False
    assert body["dashboard_url"] == "https://leash.example.com/"
    assert all(not isinstance(value, (dict, list)) for value in body.values())


def test_blocked_attack_is_flagged(tmp_path, zapier):
    make_gate(tmp_path).process(req("b-1", amount="500.00", email="attacker@example.com"))
    events.drain()

    [(_, body)] = zapier
    assert body["event"] == "decision.blocked"
    assert body["is_attack"] is True
    assert "allowlist" in body["attack_reasons"]


def test_price_inflation_carries_the_channel3_price(tmp_path, zapier):
    catalog = {
        "status": "ok", "product_id": "prod_1", "name": "Birthday Bear",
        "sold_by": "cheap.example", "merchant_email": SELLER, "price_usd": "4.99",
    }
    make_gate(tmp_path, catalog).process(req("p-1", amount="9.00", product_id="prod_1"))
    events.drain()

    [(_, body)] = zapier
    assert body["event"] == "decision.blocked"
    assert body["is_attack"] is True
    assert body["attack_reasons"] == "price_mismatch"
    assert body["channel3_price_usd"] == "4.99"
    assert body["channel3_sold_by"] == "cheap.example"


def test_paid_payments_are_not_sent_by_default(tmp_path, zapier):
    make_gate(tmp_path).process(req("a-1"))
    events.drain()
    assert zapier == []


def test_no_token_ever_leaves_in_the_payload(tmp_path, zapier):
    gate = make_gate(tmp_path)
    gate.process(req("h-1", amount="15.00"))
    gate.process(req("b-1", amount="500.00", email="attacker@example.com"))
    events.drain()

    raw = json.dumps(zapier)
    assert "secret-dashboard-token-123" not in raw
    assert "token" not in raw.lower()


def test_a_zapier_outage_does_not_change_the_decision(tmp_path):
    def down(request):
        return httpx.Response(503)

    events.register(sinks.ZapierSink(
        HOOK, http_client=httpx.Client(transport=httpx.MockTransport(down))
    ))
    record = make_gate(tmp_path).process(req("h-1", amount="15.00"))
    events.drain()
    assert record["status"] == "pending"


def test_from_env_installs_zapier_only_when_set(monkeypatch):
    monkeypatch.delenv("ZAPIER_HOOK_URL", raising=False)
    assert [name for name, _ in sinks.from_env()] == []

    monkeypatch.setenv("ZAPIER_HOOK_URL", HOOK)
    [(name, sink)] = sinks.from_env()
    assert name == "zapier"
    assert sink.kinds == {"decision.held", "decision.blocked"}

    monkeypatch.setenv("ZAPIER_ALL_EVENTS", "true")
    [(_, sink)] = sinks.from_env()
    assert sink.kinds is None


def test_plain_http_hook_is_refused(monkeypatch):
    monkeypatch.setenv("ZAPIER_HOOK_URL", "http://hooks.zapier.com/x")
    assert sinks.from_env() == []
