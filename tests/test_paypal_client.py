from __future__ import annotations

import json

import httpx

from payouts.paypal_client import PayPalClient


def test_same_idempotency_key_sends_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(
            201,
            json={"batch_header": {"payout_batch_id": "batch-1", "batch_status": "PENDING"}},
        )

    transport = httpx.MockTransport(handler)
    with PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=transport),
    ) as client:
        first = client.send_payout("key-1", "receiver@example.com", 1, "Test")
        second = client.send_payout("key-1", "receiver@example.com", 1, "Test")

    assert first == second
    assert [request.url.path for request in calls].count("/v1/payments/payouts") == 1


def test_self_pay_error_is_recorded_as_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "client-secret")

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(400, json={"name": "SELF_PAY_NOT_ALLOWED"})

    with PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as client:
        result = client.send_payout("key-2", "same@example.com", "1.00", "Test")

    assert result["status"] == "failed"
    assert result["error_name"] == "SELF_PAY_NOT_ALLOWED"


def test_dry_run_uses_no_network(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")

    def fail_if_called(request):
        raise AssertionError(f"network call made: {request}")

    with PayPalClient(
        db_path=str(tmp_path / "payouts.db"),
        http_client=httpx.Client(transport=httpx.MockTransport(fail_if_called)),
    ) as client:
        payout = client.send_payout("key-3", "receiver@example.com", "1.00", "Test")
        status = client.wait_for_final_status(payout["paypal_batch_id"])

    assert payout["status"] == "success"
    assert status["batch_status"] == "SUCCESS"
    assert status["transaction_status"] == "SUCCESS"


