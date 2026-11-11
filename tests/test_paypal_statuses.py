"""Every payout status in PayPal's Payouts v1 schema ends somewhere sensible.

The lists below are copied from PayPal's published schema
(developer.paypal.com/api/payments.payouts-batch/v1/schema.json):
batch_enum and transaction_enum. A status Leash did not know used to leave a
payment stuck on "Checking with PayPal" forever.
"""

from __future__ import annotations

import httpx
import pytest

from leash.gate import IN_PROGRESS_STATUSES, PAYPAL_STATUS_MAP, PaymentGate
from payouts.paypal_client import FINAL_STATUSES, PayPalClient
from test_agent import FakePayout, allowed_request


PAYPAL_BATCH_STATUSES = ["DENIED", "PENDING", "PROCESSING", "SUCCESS", "CANCELED"]
PAYPAL_ITEM_STATUSES = [
    "SUCCESS", "FAILED", "PENDING", "UNCLAIMED", "RETURNED",
    "ONHOLD", "BLOCKED", "REFUNDED", "REVERSED",
]


@pytest.mark.parametrize("status", sorted(set(PAYPAL_BATCH_STATUSES + PAYPAL_ITEM_STATUSES)))
def test_every_paypal_status_is_either_final_or_in_progress(status):
    assert (status in PAYPAL_STATUS_MAP) != (status in IN_PROGRESS_STATUSES)


class SubmittedPayPal(FakePayout):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "submitted", "paypal_batch_id": "batch-x"}

    def get_payout_status(self, batch_id):
        return self.answer


def run_one_poll(tmp_path, answer):
    payout = SubmittedPayPal(answer)
    with PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
    ) as gate:
        gate.process(allowed_request(key="r1"))
        results = gate.poll_submitted()
        return results, gate.get("r1"), gate.daily_spend_usd(), payout


@pytest.mark.parametrize("status", ["RETURNED", "REFUNDED", "REVERSED", "BLOCKED"])
def test_money_that_never_arrived_or_came_back_is_failed_and_not_spend(tmp_path, status):
    results, record, spend, payout = run_one_poll(
        tmp_path, {"batch_status": "SUCCESS", "transaction_status": status, "error_name": None}
    )

    assert [result["status"] for result in results] == ["failed"]
    assert record["status"] == "failed"
    assert record["error_name"] == status  # the log says why
    assert spend == 0
    assert len(payout.calls) == 1  # failing never sends it again


def test_canceled_batch_is_failed(tmp_path):
    _, record, spend, _ = run_one_poll(
        tmp_path, {"batch_status": "CANCELED", "transaction_status": None, "error_name": None}
    )
    assert record["status"] == "failed"
    assert record["error_name"] == "CANCELED"
    assert spend == 0


def test_on_hold_item_stays_submitted_and_still_counts_as_spend(tmp_path):
    _, record, spend, _ = run_one_poll(
        tmp_path, {"batch_status": "SUCCESS", "transaction_status": "ONHOLD", "error_name": None}
    )
    assert record["status"] == "submitted"
    assert spend == 8


def test_paypal_error_name_wins_over_the_status(tmp_path):
    _, record, _, _ = run_one_poll(
        tmp_path,
        {"batch_status": "SUCCESS", "transaction_status": "BLOCKED", "error_name": "RECEIVER_BLOCKED"},
    )
    assert record["error_name"] == "RECEIVER_BLOCKED"


def test_client_wait_stops_on_a_returned_item(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    status_calls = []

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(
                201, json={"batch_header": {"payout_batch_id": "b-1", "batch_status": "PENDING"}}
            )
        status_calls.append(request)
        return httpx.Response(200, json={
            "batch_header": {"batch_status": "SUCCESS"},
            "items": [{"transaction_status": "RETURNED"}],
        })

    with PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: pytest.fail("should not wait on a final status"),
    ) as client:
        client.send_payout("key-r", "receiver@example.com", "1.00", "Test")
        client.wait_for_final_status("b-1", timeout_s=30)
        stored = client._get_record("key-r")

    assert "RETURNED" in FINAL_STATUSES
    assert len(status_calls) == 1
    assert stored["status"] == "failed"
    assert stored["error_name"] == "RETURNED"


def test_client_keeps_waiting_while_the_item_is_on_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    item_statuses = ["ONHOLD", "SUCCESS"]

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(
                201, json={"batch_header": {"payout_batch_id": "b-2", "batch_status": "PENDING"}}
            )
        return httpx.Response(200, json={
            "batch_header": {"batch_status": "SUCCESS"},
            "items": [{"transaction_status": item_statuses.pop(0)}],
        })

    waits = []
    with PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=waits.append,
        monotonic=lambda: 0.0,
    ) as client:
        client.send_payout("key-h", "receiver@example.com", "1.00", "Test")
        client.wait_for_final_status("b-2", timeout_s=30)
        stored = client._get_record("key-h")

    assert len(waits) == 1  # waited once while ONHOLD, then saw SUCCESS
    assert stored["status"] == "success"
