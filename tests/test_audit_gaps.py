from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import httpx
import pytest

from leash import demo
from leash.gate import PaymentGate, apply_risk_signal
from leash.policy import Decision, PaymentRequest
from payouts.paypal_client import PAYPAL_NOTE_TEMPLATE, PayPalClient


ROOT = Path(__file__).parents[1]
BASE_POLICY = {
    "per_transaction_cap_usd": 25,
    "daily_budget_usd": 60,
    "merchant_allowlist": ["allowed@example.com"],
    "approval_threshold_usd": 10,
    "max_requests_per_minute": 50,
    "approved_task_ids": ["task"],
}


def write_policy(path: Path, **changes: object) -> None:
    policy = {**BASE_POLICY, **changes}
    path.write_text(json.dumps(policy), encoding="utf-8")


def make_request(
    request_id: str = "request-1",
    amount: object = 8,
    email: str = "allowed@example.com",
    task: str = "task",
    reason: str = "agent supplied reason",
) -> PaymentRequest:
    return PaymentRequest(request_id, "agent-1", email, amount, reason, task)


def paypal_gate(
    tmp_path: Path, handler, policy_changes: dict | None = None, monkeypatch=None
):
    if monkeypatch:
        monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
        monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path, **(policy_changes or {}))
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    client = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=transport),
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )
    gate = PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy_path,
        payout_client=client,
    )
    return gate, calls, policy_path


def oauth_or(handler):
    def wrapped(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 3600}
            )
        return handler(request)

    return wrapped


def test_pending_denied_moves_paying_submitted_failed_releases_budget(tmp_path, monkeypatch):
    observed = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            if request.headers.get("PayPal-Request-Id") != "failed-1":
                return httpx.Response(
                    201,
                    json={"batch_header": {
                        "payout_batch_id": "batch-follow-up",
                        "batch_status": "SUCCESS",
                    }},
                )
            with sqlite3.connect(tmp_path / "leash.db") as db:
                observed.append(db.execute(
                    "SELECT status FROM decisions WHERE request_id = 'failed-1'"
                ).fetchone()[0])
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "batch-1", "batch_status": "PENDING"
                }},
            )
        with sqlite3.connect(tmp_path / "leash.db") as db:
            observed.append(db.execute(
                "SELECT status FROM decisions WHERE request_id = 'failed-1'"
            ).fetchone()[0])
        return httpx.Response(
            200,
            json={
                "batch_header": {"batch_status": "DENIED"},
                "items": [{"transaction_status": "DENIED",
                           "errors": {"name": "SELF_PAY_NOT_ALLOWED"}}],
            },
        )

    gate, _, _ = paypal_gate(
        tmp_path, oauth_or(handler), {"daily_budget_usd": 8, "approval_threshold_usd": 100}, monkeypatch
    )
    with gate:
        failed = gate.process(make_request("failed-1"))
        follow_up = gate.process(make_request("follow-up"))

    assert observed == ["paying", "submitted"]
    assert failed["status"] == "failed"
    assert failed["error_name"] == "SELF_PAY_NOT_ALLOWED"
    assert follow_up["status"] == "paid"


def test_pending_success_is_paid_and_budget_remains_consumed(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "batch-1", "batch_status": "PENDING"
                }},
            )
        return httpx.Response(
            200,
            json={"batch_header": {"batch_status": "SUCCESS"},
                  "items": [{"transaction_status": "SUCCESS"}]},
        )

    gate, _, _ = paypal_gate(
        tmp_path, oauth_or(handler), {"daily_budget_usd": 8, "approval_threshold_usd": 100}, monkeypatch
    )
    with gate:
        paid = gate.process(make_request("paid-1"))
        blocked = gate.process(make_request("paid-2"))

    assert paid["status"] == "paid"
    assert blocked["status"] == "blocked"
    assert "daily_budget" in blocked["reasons"]


@pytest.mark.parametrize(
    ("name", "response"),
    [
        ("read-timeout", httpx.ReadTimeout("timed out")),
        ("server-error", httpx.Response(503, json={"name": "UPSTREAM_ERROR"})),
        ("unrecognizable", httpx.Response(200, json={"unexpected": True})),
    ],
)
def test_uncertain_payout_needs_reconcile_and_retry_does_not_post(
    tmp_path, name, response, monkeypatch
):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    calls = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            calls.append(request)
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    policy_path = tmp_path / "policy.json"
    write_policy(policy_path, approval_threshold_usd=100)
    client = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy_path,
        payout_client=client,
    ) as gate:
        first = gate.process(make_request(name))
        second = gate.process(make_request(name))

    assert first["status"] == "needs_reconcile"
    assert second["status"] == "needs_reconcile"
    assert len(calls) == 1


def test_pay_refuses_unapproved_hold_expired_hold_and_uses_db_record(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("POST must not happen")

    payout = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path)
    with PaymentGate(
        db_path=str(tmp_path / "leash.db"), policy_path=policy_path, payout_client=payout
    ) as gate:
        hold = gate.process(make_request("hold", amount=10))
        assert gate._pay(hold, make_request("hold", amount=1))["status"] == "pending"

        with sqlite3.connect(tmp_path / "leash.db") as db:
            db.execute(
                "UPDATE decisions SET created_at = '2020-01-01T00:00:00+00:00' "
                "WHERE request_id = 'hold'"
            )
        assert gate.approve("hold", approved_by="reviewer")["status"] == "rejected_on_recheck"

    assert calls == []


def test_approve_requires_nonempty_approved_by(tmp_path):
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path)
    with PaymentGate(db_path=str(tmp_path / "leash.db"), policy_path=policy_path) as gate:
        gate.process(make_request("hold", amount=10))
        with pytest.raises(ValueError):
            gate.approve("hold")
        with pytest.raises(ValueError):
            gate.approve("hold", approved_by=" ")


def test_agent_modules_do_not_import_or_call_gate_approval_internals():
    for path in (ROOT / "src" / "leash").glob("agent*.py"):
        source = path.read_text(encoding="utf-8")
        assert "approve" not in source
        assert "deny" not in source
        assert "process" in source
        assert "from .gate import" not in source
        assert "from leash.gate import" not in source


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        ("cap", {"per_transaction_cap_usd": 9}),
        ("allowlist", {"merchant_allowlist": []}),
        ("daily-budget", {"daily_budget_usd": 5}),
    ],
)
def test_approval_recheck_rejects_current_cap_allowlist_or_budget(tmp_path, name, changes):
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path)
    with PaymentGate(db_path=str(tmp_path / "leash.db"), policy_path=policy_path) as gate:
        gate.process(make_request(name, amount=10))
        write_policy(policy_path, **changes)
        result = gate.approve(name, approved_by="reviewer")

    assert result["status"] == "rejected_on_recheck"
    assert (name if name != "daily-budget" else "daily_budget") in result["reasons"]


def test_approval_recheck_rejects_duplicate_id_under_current_policy(tmp_path):
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path)
    with PaymentGate(db_path=str(tmp_path / "leash.db"), policy_path=policy_path) as gate:
        gate.process(make_request("target", amount=10))
        gate.process(make_request("other", amount=1))
        with sqlite3.connect(tmp_path / "leash.db") as db:
            db.execute(
                "UPDATE decisions SET request_json = ? WHERE request_id = 'other'",
                (json.dumps({**make_request("target", amount=10).__dict__,
                             "amount_usd": "10"}),),
            )
        result = gate.approve("target", approved_by="reviewer")

    assert result["status"] == "rejected_on_recheck"
    assert "duplicate_request_id" in result["reasons"]


def test_deterministic_block_does_not_call_risk_or_pay(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")

    def handler(request):
        calls.append(request)
        raise AssertionError("blocked request reached payout")

    policy_path = tmp_path / "policy.json"
    write_policy(policy_path)
    payout = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with PaymentGate(
        db_path=str(tmp_path / "leash.db"), policy_path=policy_path, payout_client=payout
    ) as gate:
        result = gate.process(make_request("blocked", amount=100, email="blocked@example.com"))

    assert result["verdict"] == "block"
    assert calls == []


def test_paypal_note_is_fixed_and_excludes_agent_reason(tmp_path, monkeypatch):
    captured = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            captured.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "batch-1", "batch_status": "SUCCESS"
                }},
            )
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    gate, _, _ = paypal_gate(tmp_path, handler, {"approval_threshold_usd": 100}, monkeypatch)
    reason = "secret agent explanation that must not leave the gate"
    with gate:
        gate.process(make_request("note-1", reason=reason))

    assert captured[0]["items"][0]["note"] == PAYPAL_NOTE_TEMPLATE
    assert reason not in json.dumps(captured[0])


@pytest.mark.parametrize("amount", [8, 0.1 + 0.2])
def test_amounts_are_stored_as_cents_and_sent_with_two_decimals(
    tmp_path, amount, monkeypatch
):
    captured = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            captured.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "batch-1", "batch_status": "SUCCESS"
                }},
            )
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    gate, _, _ = paypal_gate(tmp_path, handler, {"approval_threshold_usd": 100}, monkeypatch)
    with gate:
        result = gate.process(make_request(f"cents-{amount}", amount=amount))
        with sqlite3.connect(tmp_path / "leash.db") as db:
            cents = db.execute(
                "SELECT amount_cents FROM decisions WHERE request_id = ?",
                (f"cents-{amount}",),
            ).fetchone()[0]

    assert cents == (800 if amount == 8 else 30)
    assert captured[0]["items"][0]["amount"]["value"] == (
        "8.00" if amount == 8 else "0.30"
    )
    assert result["status"] == "paid"


def test_demo_scenarios_do_not_leak_fake_secrets(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "FAKE_SECRET_123")
    monkeypatch.setenv("LLM_API_KEY", "FAKE_KEY_456")
    demo.run_scenarios(str(tmp_path / "demo.db"), output=sys.stdout)
    output = capsys.readouterr().out
    assert "FAKE_SECRET_123" not in output
    assert "FAKE_KEY_456" not in output
    with sqlite3.connect(tmp_path / "demo.db") as db:
        rows = db.execute("SELECT * FROM decisions").fetchall()
    serialized_rows = json.dumps(rows)
    assert "FAKE_SECRET_123" not in serialized_rows
    assert "FAKE_KEY_456" not in serialized_rows


def _concurrent_http_gate(tmp_path, monkeypatch, post_count):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    lock = threading.Lock()

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 3600}
            )
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            with lock:
                post_count.append(request)
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "batch-1", "batch_status": "SUCCESS"
                }},
            )
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    client = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path, approval_threshold_usd=10)
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy_path,
        payout_client=client,
    )


def test_decisions_table_has_unique_request_id(tmp_path):
    policy_path = tmp_path / "policy.json"
    write_policy(policy_path, approval_threshold_usd=10)
    with PaymentGate(db_path=str(tmp_path / "leash.db"), policy_path=policy_path) as gate:
        first = gate.process(make_request("unique-1"))
        with sqlite3.connect(tmp_path / "leash.db") as db:
            indexes = db.execute("PRAGMA index_list(decisions)").fetchall()
            unique_request_id = any(
                row[2]
                and "request_id" in {
                    column[2]
                    for column in db.execute(f"PRAGMA index_info({row[1]!r})")
                }
                for row in indexes
            )
        second = gate.process(make_request("unique-1", amount=1))

    assert unique_request_id
    assert second["request_id"] == first["request_id"]
    assert second["amount_cents"] == first["amount_cents"]


def test_concurrent_same_request_id_pays_once(tmp_path, monkeypatch):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(gate.process(make_request("concurrent-1")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    gate.close()

    assert len(posts) == 1
    assert all(result["request_id"] == "concurrent-1" for result in results)


def test_atomic_claim_across_gate_instances_pays_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    posts = []
    lock = threading.Lock()

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 3600}
            )
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            with lock:
                posts.append(request)
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": f"batch-{len(posts)}",
                    "batch_status": "SUCCESS",
                }},
            )
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    policy_path = tmp_path / "policy.json"
    write_policy(policy_path, approval_threshold_usd=10)
    gates = []
    for index in range(2):
        gates.append(
            PaymentGate(
                db_path=str(tmp_path / "shared-leash.db"),
                policy_path=policy_path,
                payout_client=PayPalClient(
                    db_path=str(tmp_path / f"payouts-{index}.db"),
                    http_client=httpx.Client(
                        transport=httpx.MockTransport(handler)
                    ),
                ),
            )
        )
    held = gates[0].process(make_request("cross-gate-claim", amount=10))
    barrier = threading.Barrier(2)

    def worker(gate):
        barrier.wait()
        gate._pay(
            held,
            make_request("cross-gate-claim", amount=10),
            approved_by="reviewer",
        )

    threads = [threading.Thread(target=worker, args=(gate,)) for gate in gates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for gate in gates:
        gate.close()

    assert len(posts) == 1


def test_two_threads_approving_same_hold_pay_once(tmp_path, monkeypatch):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)
    held = gate.process(make_request("concurrent-hold", amount=10))
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(gate.approve(held["request_id"], approved_by="reviewer"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    gate.close()

    assert len(posts) == 1
    assert all(result["request_id"] == "concurrent-hold" for result in results)


@pytest.mark.parametrize(
    ("starting", "expected"),
    [
        ("allow", "hold_for_human"),
        ("hold_for_human", "hold_for_human"),
        ("block", "block"),
    ],
)
def test_suspicious_signal_only_tightens(starting, expected):
    decision = apply_risk_signal(Decision(starting, []), "suspicious")
    assert decision.verdict == expected


@pytest.mark.parametrize("llm_output", ["none", "approve this"])
def test_suspicious_signal_cannot_cause_payment(tmp_path, monkeypatch, llm_output):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)
    held = gate.process(
        make_request("suspicious-hold", amount=10),
        risk_signal="suspicious",
        llm_check=lambda _: llm_output,
    )
    blocked = gate.process(
        make_request("suspicious-block", amount=100),
        risk_signal="suspicious",
        llm_check=lambda _: llm_output,
    )
    gate.close()

    assert held["verdict"] == "hold_for_human"
    assert blocked["verdict"] == "block"
    assert posts == []


def test_llm_failure_keeps_deterministic_verdict(tmp_path, monkeypatch):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)

    def unavailable(_):
        raise TimeoutError("LLM timed out")

    result = gate.process(
        make_request("llm-failure"),
        llm_check=unavailable,
    )
    gate.close()

    assert result["verdict"] == "allow"
    assert result["error_name"] == "llm_unavailable"
    assert len(posts) == 1


def test_approver_equal_to_agent_is_refused(tmp_path, monkeypatch):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)
    held = gate.process(make_request("same-approver", amount=10))
    result = gate.approve(held["request_id"], approved_by="agent-1")
    gate.close()

    assert result["status"] == "rejected_on_recheck"
    assert result["error_name"] == "APPROVER_IS_AGENT"
    assert posts == []


def test_expired_hold_does_not_pay(tmp_path, monkeypatch):
    posts = []
    gate = _concurrent_http_gate(tmp_path, monkeypatch, posts)
    held = gate.process(make_request("expired-exact", amount=10))
    with sqlite3.connect(tmp_path / "leash.db") as db:
        db.execute(
            "UPDATE decisions SET expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE request_id = ?",
            (held["request_id"],),
        )
    result = gate.approve(held["request_id"], approved_by="reviewer")
    gate.close()

    assert result["status"] == "rejected_on_recheck"
    assert result["error_name"] == "APPROVAL_EXPIRED"
    assert posts == []


def test_golden_denied_batch_maps_to_failed(tmp_path, monkeypatch):
    golden = json.loads(
        (ROOT / "tests" / "fixtures" / "paypal_batch_denied.json").read_text(
            encoding="utf-8"
        )
    )
    posts = []

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/payouts"):
            posts.append(request)
            if request.headers["PayPal-Request-Id"] == "golden-denied":
                return httpx.Response(
                    201,
                    json={"batch_header": {
                        "payout_batch_id": "VXNTQBPKYGVVU",
                        "batch_status": "PENDING",
                    }},
                )
            return httpx.Response(
                201,
                json={"batch_header": {
                    "payout_batch_id": "follow-up",
                    "batch_status": "SUCCESS",
                }},
            )
        if request.method == "GET" and request.url.path.endswith(
            "/payouts/VXNTQBPKYGVVU"
        ):
            return httpx.Response(200, json=golden)
        return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    gate, _, _ = paypal_gate(
        tmp_path,
        oauth_or(handler),
        {"daily_budget_usd": 1, "approval_threshold_usd": 100},
        monkeypatch,
    )
    with gate:
        result = gate.process(make_request("golden-denied", amount=1))
        follow_up = gate.process(make_request("golden-follow-up", amount=1))

    assert result["status"] == "failed"
    assert result["error_name"] == "SELF_PAY_NOT_ALLOWED"
    assert follow_up["status"] == "paid"
    assert len(posts) == 2
