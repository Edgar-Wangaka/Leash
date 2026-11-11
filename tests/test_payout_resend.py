"""A payout PayPal never answered is recovered by sending it again with the same IDs.

PayPal's Payouts API has no search by sender_batch_id. Its documented recovery
is to send the same request again: a sender_batch_id used in the last 30 days
is never paid twice, and PayPal-Request-Id keys are kept for 30 days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from leash import gate as gate_module
from leash.gate import MAX_RESENDS, PaymentGate
from payouts.paypal_client import PayPalClient
from test_agent import FakePayout, allowed_request


# --- the PayPal client --------------------------------------------------------

@pytest.fixture
def paypal_env(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    monkeypatch.delenv("DRY_RUN", raising=False)


def client_with(tmp_path, answers):
    """A client whose payout POSTs get the given answers in order."""
    posts = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        posts.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, posts


def created(batch_id="batch-9", status="PENDING"):
    return httpx.Response(
        201, json={"batch_header": {"payout_batch_id": batch_id, "batch_status": status}}
    )


def test_resend_after_a_timeout_uses_the_same_ids_and_body(tmp_path, paypal_env):
    client, posts = client_with(
        tmp_path, [httpx.ReadTimeout("no answer"), created()]
    )
    with client:
        first = client.send_payout("req-1", "seller@example.com", "8.00", "Leash payment req-1")
        again = client.resend_payout("req-1", "seller@example.com", "8.00", "Leash payment req-1")

    assert first["status"] == "needs_reconcile" and first["paypal_batch_id"] is None
    assert again["status"] == "submitted" and again["paypal_batch_id"] == "batch-9"
    assert [p.headers["PayPal-Request-Id"] for p in posts] == ["req-1", "req-1"]
    assert posts[0].content == posts[1].content  # same sender_batch_id, same item


def test_resend_reads_the_original_batch_from_paypals_duplicate_error(tmp_path, paypal_env):
    duplicate = httpx.Response(400, json={
        "name": "USER_BUSINESS_ERROR",
        "details": [{"field": "SENDER_BATCH_ID", "issue": "Batch with given sender_batch_id already exists"}],
        "links": [{"href": "https://api-m.sandbox.paypal.com/v1/payments/payouts/ORIGINAL1",
                   "rel": "self", "method": "GET"}],
    })
    client, _ = client_with(tmp_path, [httpx.ConnectError("down"), duplicate])
    with client:
        client.send_payout("req-2", "seller@example.com", "8.00", "n")
        again = client.resend_payout("req-2", "seller@example.com", "8.00", "n")

    assert again["status"] == "submitted"
    assert again["paypal_batch_id"] == "ORIGINAL1"
    assert again["error_name"] is None


@pytest.mark.parametrize("answer", [
    httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
    httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}),
])
def test_resend_stays_uncertain_on_a_5xx_or_an_auth_error(tmp_path, paypal_env, answer):
    client, _ = client_with(tmp_path, [httpx.ReadTimeout("x"), answer])
    with client:
        client.send_payout("req-3", "seller@example.com", "8.00", "n")
        again = client.resend_payout("req-3", "seller@example.com", "8.00", "n")
    assert again["status"] == "needs_reconcile"
    assert again["paypal_batch_id"] is None


def test_a_plain_refusal_on_resend_is_failed(tmp_path, paypal_env):
    refused = httpx.Response(422, json={"name": "INSUFFICIENT_FUNDS"})
    client, _ = client_with(tmp_path, [httpx.ReadTimeout("x"), refused])
    with client:
        client.send_payout("req-4", "seller@example.com", "8.00", "n")
        again = client.resend_payout("req-4", "seller@example.com", "8.00", "n")
    assert again["status"] == "failed"
    assert again["error_name"] == "INSUFFICIENT_FUNDS"


def test_resend_does_not_post_when_the_batch_is_already_known(tmp_path, paypal_env):
    client, posts = client_with(tmp_path, [created("batch-known")])
    with client:
        client.send_payout("req-5", "seller@example.com", "8.00", "n")
        again = client.resend_payout("req-5", "seller@example.com", "8.00", "n")
    assert again["paypal_batch_id"] == "batch-known"
    assert len(posts) == 1


def test_find_payout_batch_is_local_only(tmp_path, paypal_env):
    # PayPal has no search by sender_batch_id; the old call went to an
    # endpoint that is not in its schema.
    client, posts = client_with(tmp_path, [])
    with client:
        assert client.find_payout_batch("never-sent") is None
    assert posts == []


# --- the gate -----------------------------------------------------------------

class UnansweredPayPal(FakePayout):
    """The first send gets no answer; resends get the given answers."""

    def __init__(self, resend_answers):
        super().__init__()
        self.resend_answers = list(resend_answers)
        self.resends = []
        self.status_checks = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "needs_reconcile", "paypal_batch_id": None}

    def resend_payout(self, *args):
        self.resends.append(args)
        return self.resend_answers.pop(0)

    def get_payout_status(self, batch_id):
        self.status_checks.append(batch_id)
        return {"batch_status": "SUCCESS", "transaction_status": "SUCCESS", "error_name": None}


def make_gate(tmp_path, payout, **kwargs):
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
        **kwargs,
    )


def test_an_unanswered_payout_is_resent_once_then_followed_to_paid(tmp_path):
    payout = UnansweredPayPal([{"status": "submitted", "paypal_batch_id": "batch-r"}])
    with make_gate(tmp_path, payout) as gate:
        first = gate.process(allowed_request(key="r1"))
        resent = gate.poll_submitted()
        settled = gate.poll_submitted()
        record = gate.get("r1")

    assert first["status"] == "needs_reconcile"
    assert [r["status"] for r in resent] == ["submitted"]
    assert [r["status"] for r in settled] == ["paid"]
    assert len(payout.calls) == 1 and len(payout.resends) == 1
    receiver, amount, note = payout.resends[0][1:]
    assert payout.calls[0][1:] == (receiver, amount, note)  # same body as the first send
    assert payout.status_checks == ["batch-r"]
    assert record["payout"]["resend_attempts"] == 1
    assert record["payout"]["paypal_batch_id"] == "batch-r"


def test_resending_stops_after_the_limit(tmp_path):
    answers = [{"status": "needs_reconcile", "paypal_batch_id": None}] * (MAX_RESENDS + 3)
    payout = UnansweredPayPal(answers)
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r2"))
        results = [gate.poll_submitted()[0] for _ in range(MAX_RESENDS + 2)]

    assert len(payout.resends) == MAX_RESENDS
    assert results[-1]["status"] == "needs_reconcile"
    assert results[-1]["reason"] == "resend_limit_reached"


def test_no_resend_once_the_payout_is_a_week_old(tmp_path, monkeypatch):
    payout = UnansweredPayPal([{"status": "submitted", "paypal_batch_id": "late"}])
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r3"))
        later = datetime.now(timezone.utc) + timedelta(days=8)
        monkeypatch.setattr(gate, "_now", lambda: later)
        result = gate.poll_submitted()[0]

    assert payout.resends == []
    assert result["reason"] == "too_old_to_resend"


def test_no_resend_when_the_rules_no_longer_allow_it_and_it_is_not_rejected(tmp_path):
    payout = UnansweredPayPal([{"status": "submitted", "paypal_batch_id": "x"}])
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r4"))
        changed = gate.change_settings(
            {"merchant_allowlist": ["someone-else@example.com"]},
            changed_by="test", confirm_loosening=True,
        )
        assert changed["ok"], changed
        result = gate.poll_submitted()[0]
        record = gate.get("r4")

    assert payout.resends == []
    assert result["reason"] == "resend_blocked:allowlist"
    # The first send may still have paid, so it is not marked rejected.
    assert record["status"] == "needs_reconcile"


def test_a_refused_resend_is_failed_and_is_not_spend(tmp_path):
    payout = UnansweredPayPal([
        {"status": "failed", "paypal_batch_id": None, "error_name": "INSUFFICIENT_FUNDS"}
    ])
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r5"))
        result = gate.poll_submitted()[0]
        record = gate.get("r5")
        spend = gate.daily_spend_usd()

    assert result["status"] == "failed"
    assert record["error_name"] == "INSUFFICIENT_FUNDS"
    assert spend == 0


def test_a_client_without_resend_keeps_the_old_answer(tmp_path):
    class OldClient(FakePayout):
        def send_payout(self, *args):
            self.calls.append(args)
            return {"status": "needs_reconcile", "paypal_batch_id": None}

    payout = OldClient()
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r6"))
        result = gate.poll_submitted()[0]
    assert result["reason"] == "batch_id_unknown"
    assert len(payout.calls) == 1


def test_a_submitted_payout_is_never_resent(tmp_path):
    class Submitted(UnansweredPayPal):
        def send_payout(self, *args):
            self.calls.append(args)
            return {"status": "submitted", "paypal_batch_id": "b-known"}

    payout = Submitted([])
    with make_gate(tmp_path, payout) as gate:
        gate.process(allowed_request(key="r7"))
        gate.poll_submitted()
    assert payout.resends == []
    assert payout.status_checks == ["b-known"]
