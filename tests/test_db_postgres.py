"""The payment log in Postgres behaves like the SQLite one.

The translation tests always run. The database tests run only when
LEASH_TEST_DATABASE_URL points at a disposable Postgres (a Neon branch, never
the production database); each run works in its own schema and drops it.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from leash.db import PostgresDatabase, SqliteDatabase, schema_name, translate
from leash.gate import PaymentGate
from leash.policy import PaymentRequest
from payouts.paypal_client import PayPalClient


# --- translation (no database needed) -------------------------------------------

def test_json_paths_become_jsonb_operators():
    assert translate("SELECT json_extract(request_json, '$.catalog.sold_by') FROM d") == (
        "SELECT (request_json::jsonb #>> '{catalog,sold_by}') FROM d"
    )


def test_is_not_a_parameter_becomes_is_distinct_from():
    assert translate("WHERE request_id IS NOT ?") == "WHERE request_id IS DISTINCT FROM %s"


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    assert translate("INSERT OR IGNORE INTO t (a) VALUES (?)") == (
        "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING"
    )


def test_rowid_and_percent_signs():
    assert translate("ORDER BY created_at DESC, rowid DESC") == "ORDER BY created_at DESC, seq DESC"
    assert translate("SELECT '5%'") == "SELECT '5%%'"


def test_schema_names_are_safe():
    assert schema_name("leash-1pqh.onrender.com") == "leash_leash_1pqh_onrender_com"
    assert schema_name('x"; DROP SCHEMA public; --') == "leash_x_drop_schema_public"
    assert schema_name("") == "leash_local"


def test_sqlite_is_still_the_default(tmp_path):
    with PaymentGate(db_path=str(tmp_path / "l.db"), payout_client=object()) as gate:
        assert isinstance(gate._db, SqliteDatabase)


# --- against a real Postgres ----------------------------------------------------

URL = os.getenv("LEASH_TEST_DATABASE_URL", "").strip()
needs_pg = pytest.mark.skipif(not URL, reason="LEASH_TEST_DATABASE_URL is not set")
SELLER = "sb-fwgx550980251@personal.example.com"


class FakePayout:
    def __init__(self, status="success"):
        self.calls, self.status = [], status
        self._lock = threading.Lock()

    def send_payout(self, *args):
        with self._lock:
            self.calls.append(args)
        return {"status": self.status, "paypal_batch_id": f"batch-{len(self.calls)}"}

    def get_payout_status(self, batch_id):
        return {"batch_status": "SUCCESS", "transaction_status": "SUCCESS", "error_name": None}

    def close(self):
        pass


@pytest.fixture
def pg(tmp_path):
    namespace = f"test-{uuid.uuid4().hex[:10]}"
    database = PostgresDatabase(URL, namespace)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 100, "daily_budget_usd": 50,
        "merchant_allowlist": [SELLER], "approval_threshold_usd": 40,
        "max_requests_per_minute": 1000, "approved_task_ids": ["t"],
        "human_approval_expiry_hours": 24,
    }))

    def make(payout=None):
        return PaymentGate(
            db_path=str(tmp_path / "unused.db"), policy_path=policy,
            payout_client=payout or FakePayout(),
            risk_signal=lambda request, task: ("none", "none"), database=database,
        )

    yield database, make
    with database.connect() as connection:
        connection.raw.execute(f'DROP SCHEMA IF EXISTS "{database.schema}" CASCADE')
    database.close()


def request(key, amount="8.00"):
    return PaymentRequest(key, "agent-1", SELLER, amount, "gift", "t")


@needs_pg
def test_decisions_survive_a_restart(pg):
    database, make = pg
    with make() as gate:
        assert gate.process(request("a"))["status"] == "paid"
        assert gate.process(request("b", "41.00"))["status"] == "pending"  # $49 of $50
    # A new gate on the same database: what a Render restart does.
    with make() as restarted:
        ids = [d["request_id"] for d in restarted.list_decisions(10)]
        assert ids == ["b", "a"]  # newest first
        assert restarted.daily_spend_usd() == 8


@needs_pg
def test_a_repeated_request_id_is_one_decision(pg):
    _, make = pg
    with make() as gate:
        first = gate.process(request("same"))
        again = gate.process(request("same"))
        assert first["request_id"] == again["request_id"]
        assert len(gate.list_decisions(10)) == 1


@needs_pg
def test_two_approvals_at_once_pay_once(pg):
    _, make = pg
    payout = FakePayout()
    with make(payout) as gate:
        gate.process(request("hold", "45.00"))
        results = []
        threads = [threading.Thread(target=lambda: results.append(gate.approve("hold", "human")))
                   for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sum(1 for r in results if r["ok"]) == 1
    assert len(payout.calls) == 1


@needs_pg
def test_concurrent_requests_never_overspend_the_budget(pg):
    _, make = pg
    payout = FakePayout()
    with make(payout) as gate:
        threads = [threading.Thread(target=gate.process, args=(request(f"r{i}", "8.00"),))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        spend = gate.daily_spend_usd()
    assert spend <= 50          # 6 × $8 = $48 fit; the rest are blocked
    assert len(payout.calls) == 6


@needs_pg
def test_an_unanswered_payout_is_resent_and_followed(pg):
    _, make = pg

    class Unanswered(FakePayout):
        def send_payout(self, *args):
            self.calls.append(args)
            return {"status": "needs_reconcile", "paypal_batch_id": None}

        def resend_payout(self, *args):
            return {"status": "submitted", "paypal_batch_id": "batch-r"}

    with make(Unanswered()) as gate:
        assert gate.process(request("u"))["status"] == "needs_reconcile"
        assert [r["status"] for r in gate.poll_submitted()] == ["submitted"]
        assert [r["status"] for r in gate.poll_submitted()] == ["paid"]
        assert gate.get("u")["payout"]["resend_attempts"] == 1


@needs_pg
def test_price_events_are_stored_once_and_shown_on_waiting_requests(pg):
    _, make = pg
    with make() as gate:
        gate.process(PaymentRequest("h", "agent-1", SELLER, "45.00", "gift", "t", "prod_1"))
        event = {
            "id": "evt_1", "type": "offer.price.changed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {"subject": {"product_id": "prod_1", "domain": None},
                     "previous": {"price": 45.0}, "current": {"price": 30.0}},
        }
        assert gate.record_price_event(event) == {"recorded": True, "pending_request_ids": ["h"]}
        assert gate.record_price_event(event)["recorded"] is False
        assert gate.price_alerts()["h"]["price"]["current"]["price"] == 30.0
        assert gate.has_pending_hold_for("prod_1")


@needs_pg
def test_payout_records_survive_a_restart(pg, monkeypatch):
    database, _ = pg
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://paypal.test")
    monkeypatch.delenv("DRY_RUN", raising=False)
    posts = []

    def handler(http_request):
        if http_request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        posts.append(http_request)
        return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})

    def client():
        return PayPalClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                            database=database)

    with client() as first:
        first.send_payout("key-1", SELLER, "8.00", "n")
    with client() as after_restart:
        again = after_restart.send_payout("key-1", SELLER, "8.00", "n")
    assert again["paypal_batch_id"] == "B1"
    assert len(posts) == 1  # never sent twice


@needs_pg
def test_accounts_work_in_postgres(pg):
    _, make = pg
    with make() as gate:
        created = gate.create_account("Judge: Pooja", "approver", "human-dashboard")
        assert created["ok"]
        assert gate.create_account("judge: pooja", "viewer", "x")["reason"] == "name_taken"
        assert gate.account_for_token(created["token"])["role"] == "approver"
        gate.touch_account(created["account"]["id"])
        assert gate.list_accounts()[0]["last_used_at"]
        assert gate.revoke_account(created["account"]["id"], "human-dashboard")
        assert gate.account_for_token(created["token"]) is None
