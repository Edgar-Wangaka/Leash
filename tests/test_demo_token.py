"""The judges' token: everything the human can do in the dashboard, under its own name.

It is a second human token, not the agent's: the MCP token is still refused
everywhere in the dashboard, and the demo token can never equal either one.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from leash import dashboard
from leash.dashboard import DEMO_APPROVER, TOKEN_HEADER, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


HUMAN = "human-token-0123456789"
JUDGE = "judge-token-0123456789"
SELLER = "sb-fwgx550980251@personal.example.com"


class FakePayout:
    def __init__(self):
        self.calls = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


@pytest.fixture
def server(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 25, "daily_budget_usd": 60,
        "merchant_allowlist": [SELLER], "approval_threshold_usd": 10,
        "max_requests_per_minute": 100, "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
    }))
    payout = FakePayout()
    gate = PaymentGate(
        db_path=str(tmp_path / "leash.db"), policy_path=policy, payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
    )
    gate.process(PaymentRequest("held-1", "agent-1", SELLER, "15", "gift", "demo-legitimate-task"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, HUMAN, demo_token=JUDGE))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", gate, payout
    httpd.shutdown()
    httpd.server_close()
    gate.close()


def post(base, path, body, token):
    return httpx.post(f"{base}{path}", content=json.dumps(body), headers={
        "Content-Type": "application/json", TOKEN_HEADER: token,
    })


def test_a_judge_can_read_the_state(server):
    base, _, _ = server
    assert httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: JUDGE}).status_code == 200


def test_a_judge_approval_pays_and_is_recorded_as_the_judge(server):
    base, gate, payout = server
    response = post(base, "/api/approve", {"request_id": "held-1"}, JUDGE)
    assert response.status_code == 200 and response.json()["status"] == "paid"
    assert gate.get("held-1")["approved_by"] == DEMO_APPROVER
    assert len(payout.calls) == 1


def test_the_human_is_still_recorded_as_the_human(server):
    base, gate, _ = server
    post(base, "/api/approve", {"request_id": "held-1"}, HUMAN)
    assert gate.get("held-1")["approved_by"] == "human-dashboard"


def test_a_judge_limit_change_is_recorded_as_the_judge(server):
    base, gate, _ = server
    response = post(base, "/api/settings", {"changes": {"daily_budget_usd": 40}}, JUDGE)
    assert response.status_code == 200 and response.json()["reason"] == "saved"
    assert gate.settings.store.history(1)[0]["changed_by"] == DEMO_APPROVER


@pytest.mark.parametrize("token", ["", "wrong-token-0123456789", "mcp-token-0123456789"])
def test_any_other_token_is_refused(server, token):
    base, _, payout = server
    assert post(base, "/api/approve", {"request_id": "held-1"}, token).status_code == 401
    assert payout.calls == []


def test_without_a_demo_token_only_the_human_gets_in(tmp_path):
    gate = PaymentGate(db_path=str(tmp_path / "leash.db"), payout_client=FakePayout())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, HUMAN))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: JUDGE}).status_code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        gate.close()


def test_the_handler_refuses_a_demo_token_equal_to_the_human_one():
    with pytest.raises(ValueError):
        make_handler(object(), HUMAN, demo_token=HUMAN)
    with pytest.raises(ValueError):
        make_handler(object(), HUMAN, demo_token="")


# --- reading it from the environment -------------------------------------------

def test_no_demo_token_is_none(monkeypatch):
    monkeypatch.delenv("LEASH_DEMO_TOKEN", raising=False)
    assert dashboard.demo_token_from_env(HUMAN) is None


def test_the_demo_token_is_read_and_trimmed(monkeypatch):
    monkeypatch.setenv("LEASH_DEMO_TOKEN", f"  {JUDGE}  ")
    monkeypatch.setenv("LEASH_MCP_TOKEN", "mcp-token-0123456789")
    assert dashboard.demo_token_from_env(HUMAN) == JUDGE


@pytest.mark.parametrize("value, other", [
    ("short", None),
    (HUMAN, None),                          # same as the human's
    ("mcp-token-0123456789", "mcp"),        # same as the agent's
])
def test_a_bad_demo_token_stops_startup(monkeypatch, value, other):
    monkeypatch.setenv("LEASH_DEMO_TOKEN", value)
    monkeypatch.setenv("LEASH_MCP_TOKEN", "mcp-token-0123456789")
    with pytest.raises(SystemExit):
        dashboard.demo_token_from_env(HUMAN)
