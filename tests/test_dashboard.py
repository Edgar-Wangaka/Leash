"""The approval endpoints answer only the human holding the startup token."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from leash.dashboard import TOKEN_HEADER, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


TOKEN = "test-token-123"
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
        "per_transaction_cap_usd": 25,
        "daily_budget_usd": 60,
        "merchant_allowlist": [SELLER],
        "approval_threshold_usd": 10,
        "max_requests_per_minute": 100,
        "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
    }))
    payout = FakePayout()
    gate = PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy,
        payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
    )
    gate.process(PaymentRequest(
        "held-1", "agent-1", SELLER, "15", "gift", "demo-legitimate-task"
    ))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", port, payout
    httpd.shutdown()
    httpd.server_close()


def approve(base, headers):
    return httpx.post(
        f"{base}/api/approve",
        content=json.dumps({"request_id": "held-1"}),
        headers={"Content-Type": "application/json", **headers},
    )


def test_approve_without_the_token_is_refused(server):
    base, _, payout = server
    response = approve(base, {})  # what `curl` from a local agent would send

    assert response.status_code == 401
    assert payout.calls == []


def test_approve_with_a_wrong_token_is_refused(server):
    base, _, payout = server
    assert approve(base, {TOKEN_HEADER: "guess"}).status_code == 401
    assert payout.calls == []


def test_approve_with_the_token_pays(server):
    base, _, payout = server
    response = approve(base, {TOKEN_HEADER: TOKEN})

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert len(payout.calls) == 1


def test_dns_rebinding_host_is_refused_even_with_matching_origin(server):
    base, port, payout = server
    rebound = f"evil.example:{port}"
    response = approve(
        base, {TOKEN_HEADER: TOKEN, "Host": rebound, "Origin": f"http://{rebound}"}
    )

    assert response.status_code == 403
    assert payout.calls == []


def test_cross_site_origin_is_refused(server):
    base, _, payout = server
    response = approve(base, {TOKEN_HEADER: TOKEN, "Origin": "http://evil.example"})

    assert response.status_code == 403
    assert payout.calls == []


def test_state_needs_the_token_but_the_page_does_not(server):
    base, _, _ = server
    assert httpx.get(f"{base}/api/state").status_code == 401
    assert httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: TOKEN}).status_code == 200
    assert httpx.get(f"{base}/").status_code == 200


def test_handler_refuses_an_empty_token():
    with pytest.raises(ValueError):
        make_handler(object(), "")
