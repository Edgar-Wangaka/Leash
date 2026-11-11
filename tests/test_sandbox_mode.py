"""PayPal sandbox on a hosted Leash: test money only, unless live is asked for by name."""

from __future__ import annotations

import httpx
import pytest

from leash import config
from leash.events import public_record
from payouts.paypal_client import PayPalClient


@pytest.mark.parametrize("dry, url, mode", [
    ("true", "https://api-m.paypal.com", "dry-run"),
    ("false", "https://api-m.sandbox.paypal.com", "sandbox"),
    ("false", "https://api.sandbox.paypal.com/", "sandbox"),
    ("false", "https://api-m.paypal.com", "live"),
    # Only PayPal's own sandbox host counts, not any URL that says "sandbox".
    ("false", "https://sandbox.evil.example/api-m.sandbox.paypal.com", "live"),
    ("false", "https://api-m.sandbox.paypal.com.evil.example", "live"),
])
def test_mode_comes_from_the_host_name(monkeypatch, dry, url, mode):
    monkeypatch.setenv("DRY_RUN", dry)
    monkeypatch.setenv("PAYPAL_BASE_URL", url)
    assert config.paypal_mode() == mode


def test_sandbox_and_dry_run_start(monkeypatch):
    monkeypatch.delenv("LEASH_ALLOW_LIVE", raising=False)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    config.check_live_money()
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    config.check_live_money()


@pytest.mark.parametrize("allow", [None, "", "yes", "1", "TRUE-ish"])
def test_live_money_refuses_to_start_unless_allowed_in_so_many_words(monkeypatch, allow):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    if allow is None:
        monkeypatch.delenv("LEASH_ALLOW_LIVE", raising=False)
    else:
        monkeypatch.setenv("LEASH_ALLOW_LIVE", allow)
    with pytest.raises(SystemExit):
        config.check_live_money()


def test_live_money_starts_when_explicitly_allowed(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    monkeypatch.setenv("LEASH_ALLOW_LIVE", "true")
    config.check_live_money()


def test_payout_status_keeps_the_transaction_details(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    monkeypatch.delenv("DRY_RUN", raising=False)

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        # The shape the sandbox returned in the resend run (2026-11).
        return httpx.Response(200, json={
            "batch_header": {"payout_batch_id": "B1", "batch_status": "PROCESSING"},
            "items": [{"payout_item_id": "ITEM1", "transaction_id": "TX1",
                       "transaction_status": "SUCCESS", "time_processed": "2026-11-11T20:00:00Z"}],
        })

    with PayPalClient(db_path=str(tmp_path / "p.db"),
                      http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        status = client.get_payout_status("B1")
    assert status["transaction_id"] == "TX1"
    assert status["payout_item_id"] == "ITEM1"
    assert status["time_processed"] == "2026-11-11T20:00:00Z"


def test_the_dashboard_shape_carries_the_paypal_details():
    record = {
        "request_id": "r", "agent_id": "a", "merchant_email": "m", "amount_cents": 800,
        "request": {}, "verdict": "allow", "reasons": [], "status": "paid", "explanation": "",
        "risk_signal": "none", "approved_by": None, "error_name": None, "created_at": "t",
        "payout": {"paypal_batch_id": "B1", "transaction_id": "TX1", "batch_status": "PROCESSING",
                   "transaction_status": "SUCCESS", "time_processed": "2026-11-11T20:00:00Z"},
    }
    shown = public_record(record)
    assert shown["paypal_transaction_id"] == "TX1"
    assert shown["paypal_status"] == "SUCCESS"   # the item's status, not the batch's
    assert shown["paypal_time_processed"] == "2026-11-11T20:00:00Z"
