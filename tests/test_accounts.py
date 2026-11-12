"""Accounts: named access tokens with roles, created and revoked by the owner."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from leash import accounts, dashboard
from leash.dashboard import TOKEN_HEADER, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest

OWNER = "owner-token-0123456789"
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


def make_gate(tmp_path, payout=None):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 100, "daily_budget_usd": 250, "merchant_allowlist": [SELLER],
        "approval_threshold_usd": 40, "max_requests_per_minute": 100,
        "approved_task_ids": ["t"], "human_approval_expiry_hours": 24,
    }))
    return PaymentGate(db_path=str(tmp_path / "leash.db"), policy_path=policy,
                       payout_client=payout or FakePayout(), risk_signal=lambda r, t: ("none", "none"))


# --- the gate ----------------------------------------------------------------------

def test_a_token_is_shown_once_and_only_its_hash_is_stored(tmp_path):
    with make_gate(tmp_path) as gate:
        created = gate.create_account("Judge: Pooja", "approver", "human-dashboard")
        assert created["ok"] and created["token"].startswith("lsh_")
        assert gate.account_for_token(created["token"])["name"] == "Judge: Pooja"
        listed = gate.list_accounts()
    assert "token" not in json.dumps(listed).replace("token_hint", "")
    raw = sqlite3.connect(tmp_path / "leash.db").execute("SELECT token_hash FROM accounts").fetchone()[0]
    assert raw == accounts.token_hash(created["token"]) and created["token"] not in raw


@pytest.mark.parametrize("name, role, reason", [
    ("", "approver", "invalid_name"), ("x" * 61, "approver", "invalid_name"),
    ("Bad\x00name", "approver", "invalid_name"), ("Pooja", "admin", "invalid_role"),
])
def test_bad_accounts_are_refused(tmp_path, name, role, reason):
    with make_gate(tmp_path) as gate:
        assert gate.create_account(name, role, "owner")["reason"] == reason


def test_names_are_tidied(tmp_path):
    with make_gate(tmp_path) as gate:
        assert gate.create_account("  Judge:\n  Pooja ", "viewer", "owner")["account"]["name"] == "Judge: Pooja"


def test_names_are_unique_among_active_accounts(tmp_path):
    with make_gate(tmp_path) as gate:
        first = gate.create_account("Pooja", "viewer", "owner")
        assert gate.create_account("pooja", "viewer", "owner")["reason"] == "name_taken"
        gate.revoke_account(first["account"]["id"], "owner")
        assert gate.create_account("Pooja", "viewer", "owner")["ok"]


def test_a_revoked_or_unknown_token_finds_nobody(tmp_path):
    with make_gate(tmp_path) as gate:
        created = gate.create_account("Pooja", "approver", "owner")
        assert gate.revoke_account(created["account"]["id"], "owner")
        assert gate.revoke_account(created["account"]["id"], "owner") is False
        assert gate.account_for_token(created["token"]) is None
        assert gate.account_for_token("lsh_made-up") is None
        assert gate.account_for_token("not-even-the-prefix") is None


def test_active_accounts_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "MAX_ACTIVE_ACCOUNTS", 2)
    with make_gate(tmp_path) as gate:
        gate.create_account("A", "viewer", "owner")
        gate.create_account("B", "viewer", "owner")
        assert gate.create_account("C", "viewer", "owner")["reason"] == "too_many_accounts"


# --- the dashboard -------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    dashboard._account_cache.clear()
    payout = FakePayout()
    gate = make_gate(tmp_path, payout)
    gate.process(PaymentRequest("held-1", "agent-1", SELLER, "45.00", "gift", "t"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, OWNER, demo_token=JUDGE))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", gate, payout
    httpd.shutdown()
    httpd.server_close()
    gate.close()


def post(base, path, body, token):
    return httpx.post(f"{base}{path}", content=json.dumps(body),
                      headers={"Content-Type": "application/json", TOKEN_HEADER: token})


def state(base, token):
    return httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: token})


def create(base, name, role, token=OWNER):
    return post(base, "/api/accounts", {"name": name, "role": role}, token)


def test_the_owner_creates_an_account_and_it_signs_in(server):
    base, _, _ = server
    created = create(base, "Judge: Pooja", "approver").json()
    me = state(base, created["token"]).json()["me"]
    assert me == {"name": "Judge: Pooja", "role": "approver"}
    assert state(base, OWNER).json()["me"]["role"] == "owner"


def test_only_the_owner_manages_accounts(server):
    base, _, _ = server
    approver = create(base, "A", "approver").json()["token"]
    for token in (JUDGE, approver):
        assert create(base, "X", "owner", token).status_code == 403
        assert httpx.get(f"{base}/api/accounts", headers={TOKEN_HEADER: token}).status_code == 403
    assert create(base, "X", "owner", "nobody").status_code == 401


def test_a_viewer_can_look_but_not_act(server):
    base, gate, payout = server
    viewer = create(base, "Watcher", "viewer").json()["token"]
    assert state(base, viewer).status_code == 200
    for path, body in [("/api/approve", {"request_id": "held-1"}), ("/api/deny", {"request_id": "held-1"}),
                       ("/api/agent/run", {"task": "buy"}), ("/api/agent/hijack", {}),
                       ("/api/settings", {"reset": True}), ("/api/simulate", {"scenario": "small"})]:
        response = post(base, path, body, viewer)
        assert response.status_code == 403 and response.json()["error"] == "not_allowed", path
    assert gate.get("held-1")["status"] == "pending" and payout.calls == []


def test_an_approval_is_recorded_under_the_persons_name(server):
    base, gate, payout = server
    approver = create(base, "Judge: Pooja", "approver").json()["token"]
    assert post(base, "/api/approve", {"request_id": "held-1"}, approver).json()["status"] == "paid"
    assert gate.get("held-1")["approved_by"] == "Judge: Pooja" and len(payout.calls) == 1


def test_revoking_stops_a_token_at_once(server):
    base, _, _ = server
    created = create(base, "Pooja", "approver").json()
    assert state(base, created["token"]).status_code == 200      # now cached
    assert post(base, "/api/accounts/revoke", {"id": created["account"]["id"]}, OWNER).json()["ok"]
    assert state(base, created["token"]).status_code == 401
    assert post(base, "/api/accounts/revoke", {"id": created["account"]["id"]}, OWNER).status_code == 404


def test_the_account_list_never_contains_a_token(server):
    base, _, _ = server
    token = create(base, "Pooja", "approver").json()["token"]
    listed = httpx.get(f"{base}/api/accounts", headers={TOKEN_HEADER: OWNER}).json()
    assert listed["roles"] == ["viewer", "approver", "owner"]
    assert token not in json.dumps(listed)
    assert listed["accounts"][0]["token_hint"] == token[:8] + "…"


def test_the_judge_token_is_an_approver(server):
    base, _, _ = server
    assert state(base, JUDGE).json()["me"] == {"name": "judge-demo", "role": "approver"}


def test_unknown_routes_still_answer_401_then_404(server):
    base, _, _ = server
    assert post(base, "/api/nope", {}, "nobody").status_code == 401
    assert post(base, "/api/nope", {}, OWNER).status_code == 404
