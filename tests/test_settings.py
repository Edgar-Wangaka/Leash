"""The human can change the spending limits; the agent cannot."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from leash import mcp_server, settings
from leash.dashboard import TOKEN_HEADER, build_state, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest
from leash.settings import LiveSettings, SqliteSettingsStore, loosened, validate

SELLER = "sb-fwgx550980251@personal.example.com"
OTHER = "sb-ze5vz53545847@personal.example.com"
TOKEN = "human-token-0123456789"
FILE_POLICY = {
    "per_transaction_cap_usd": 25,
    "daily_budget_usd": 60,
    "merchant_allowlist": [SELLER],
    "approval_threshold_usd": 10,
    "max_requests_per_minute": 100,
    "human_approval_expiry_hours": 24,
    "approved_task_ids": ["demo-legitimate-task"],
    "editable_limits": {"per_transaction_cap_usd": {"max": 200}, "daily_budget_usd": {"max": 400}},
}
CURRENT = {k: FILE_POLICY[k] for k in settings.EDITABLE}


class FakePayout:
    def __init__(self):
        self.calls = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


def write_policy(tmp_path, **overrides) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**FILE_POLICY, **overrides}))
    return path


def make_gate(tmp_path, payout=None, **kwargs):
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=write_policy(tmp_path),
        payout_client=payout or FakePayout(),
        risk_signal=lambda request, task: ("none", "none"),
        **kwargs,
    )


def request(key, amount, seller=SELLER):
    return PaymentRequest(key, "agent-1", seller, amount, "gift", "demo-legitimate-task")


# --- checking a change -------------------------------------------------------------

def test_valid_change_is_applied():
    new, errors = validate(
        {"daily_budget_usd": "80.50", "max_requests_per_minute": 3,
         "merchant_allowlist": f" {SELLER.upper()}, {OTHER} "},
        CURRENT, FILE_POLICY,
    )
    assert errors == {}
    assert new["daily_budget_usd"] == 80.5
    assert new["max_requests_per_minute"] == 3
    assert new["merchant_allowlist"] == [SELLER, OTHER]


@pytest.mark.parametrize("changes, field", [
    ({"daily_budget_usd": -1}, "daily_budget_usd"),
    ({"daily_budget_usd": "1.005"}, "daily_budget_usd"),
    ({"daily_budget_usd": "NaN"}, "daily_budget_usd"),
    ({"daily_budget_usd": True}, "daily_budget_usd"),
    ({"daily_budget_usd": 401}, "daily_budget_usd"),                    # above editable max
    ({"per_transaction_cap_usd": 70}, "per_transaction_cap_usd"),       # above the budget
    ({"approval_threshold_usd": 30}, "approval_threshold_usd"),         # above the cap
    ({"max_requests_per_minute": 0}, "max_requests_per_minute"),
    ({"max_requests_per_minute": 2.5}, "max_requests_per_minute"),
    ({"human_approval_expiry_hours": 999}, "human_approval_expiry_hours"),
    ({"merchant_allowlist": ["not-an-email"]}, "merchant_allowlist"),
    ({"merchant_allowlist": "a@b.co"}, None),                           # a string is fine
    ({"merchant_allowlist": [1, 2]}, "merchant_allowlist"),
    ({"approved_task_ids": ["anything"]}, "approved_task_ids"),         # not editable
    ({"editable_limits": {}}, "editable_limits"),                       # nor are the ranges
])
def test_bad_values_are_refused(changes, field):
    _, errors = validate(changes, CURRENT, FILE_POLICY)
    assert (field in errors) if field else errors == {}


def test_loosening_is_any_higher_number_or_new_seller():
    tighter = {**CURRENT, "daily_budget_usd": 30, "merchant_allowlist": []}
    assert loosened(CURRENT, tighter) == []
    looser = {**CURRENT, "approval_threshold_usd": 12, "merchant_allowlist": [SELLER, OTHER]}
    assert loosened(CURRENT, looser) == ["approval_threshold_usd", "merchant_allowlist"]


def test_editable_maximums_default_when_policy_json_has_none():
    limits = settings.limits_for({})
    assert limits["per_transaction_cap_usd"]["max"] == 500
    assert limits["max_requests_per_minute"] == {"min": 1, "max": 60}


def test_a_ceiling_is_never_below_the_policy_json_default():
    limits = settings.limits_for({**FILE_POLICY, "max_requests_per_minute": 100,
                                  "editable_limits": {"daily_budget_usd": {"max": 10}}})
    assert limits["max_requests_per_minute"]["max"] == 100
    assert limits["daily_budget_usd"]["max"] == 60


# --- the gate uses the latest limits ------------------------------------------------

def test_tightening_applies_to_the_next_payment(tmp_path):
    payout = FakePayout()
    with make_gate(tmp_path, payout) as gate:
        assert gate.process(request("before", 8))["status"] == "paid"
        result = gate.change_settings({"approval_threshold_usd": 5}, changed_by="human")
        assert result["ok"] and result["loosened"] == []
        after = gate.process(request("after", 8))
    assert after["verdict"] == "hold_for_human"
    assert after["policy_snapshot"]["approval_threshold_usd"] == 5
    assert len(payout.calls) == 1


def test_loosening_needs_confirmation(tmp_path):
    with make_gate(tmp_path) as gate:
        refused = gate.change_settings({"per_transaction_cap_usd": 50}, changed_by="human")
        assert refused == {
            "ok": False, "reason": "confirm_loosening", "loosened": ["per_transaction_cap_usd"],
            "changes": {"per_transaction_cap_usd": {"from": 25, "to": 50}},
        }
        assert gate.policy["per_transaction_cap_usd"] == 25
        saved = gate.change_settings(
            {"per_transaction_cap_usd": 50, "daily_budget_usd": 100},
            changed_by="human", confirm_loosening=True,
        )
        assert saved["ok"] and saved["version"] == 1
        assert gate.policy["per_transaction_cap_usd"] == 50
        assert gate.process(request("big", 40))["verdict"] == "hold_for_human"


def test_reset_returns_to_policy_json(tmp_path):
    with make_gate(tmp_path) as gate:
        gate.change_settings({"daily_budget_usd": 30}, changed_by="human")
        assert gate.policy["daily_budget_usd"] == 30
        result = gate.change_settings({}, changed_by="human", reset=True, confirm_loosening=True)
        assert result["ok"] and gate.policy["daily_budget_usd"] == 60
        history = gate.settings.store.history()
    assert [entry["version"] for entry in history] == [2, 1]
    assert history[0]["changes"] == {"daily_budget_usd": {"from": 30, "to": 60}}
    assert history[0]["changed_by"] == "human"


def test_unchanged_values_save_nothing(tmp_path):
    with make_gate(tmp_path) as gate:
        result = gate.change_settings({"daily_budget_usd": "60.00"}, changed_by="human")
        assert result["reason"] == "unchanged"
        assert gate.settings.store.history() == []


def test_lowering_the_cap_stops_a_waiting_approval(tmp_path):
    payout = FakePayout()
    with make_gate(tmp_path, payout) as gate:
        assert gate.process(request("held", 20))["verdict"] == "hold_for_human"
        gate.change_settings({"per_transaction_cap_usd": 15, "approval_threshold_usd": 5},
                             changed_by="human")
        result = gate.approve("held", "human")
    assert result["status"] == "rejected_on_recheck" and "cap" in result["reason"]
    assert payout.calls == []


def test_removing_a_seller_blocks_them(tmp_path):
    with make_gate(tmp_path) as gate:
        gate.change_settings({"merchant_allowlist": []}, changed_by="human")
        result = gate.process(request("gone", 5))
    assert result["verdict"] == "block" and "allowlist" in result["reasons"]


def test_another_process_sees_the_change(tmp_path):
    with make_gate(tmp_path) as dashboard_gate, make_gate(tmp_path) as mcp_gate:
        mcp_gate.settings.ttl = 0  # the real cache is 2 s
        assert mcp_gate.policy["daily_budget_usd"] == 60
        assert dashboard_gate.change_settings({"daily_budget_usd": 30}, changed_by="human")["ok"]
        assert mcp_gate.policy["daily_budget_usd"] == 30


def test_stored_values_outside_new_ranges_fall_back_to_the_file(tmp_path):
    with make_gate(tmp_path) as gate:
        gate.change_settings({"daily_budget_usd": 300}, changed_by="human", confirm_loosening=True)
    # Someone lowers the editable maximum in policy.json afterwards.
    write_policy(tmp_path, editable_limits={"daily_budget_usd": {"max": 100}})
    with PaymentGate(
        db_path=str(tmp_path / "leash.db"), policy_path=tmp_path / "policy.json",
        payout_client=FakePayout(),
    ) as gate:
        assert gate.policy["daily_budget_usd"] == 60


class FlakyStore:
    def __init__(self, inner):
        self.inner, self.down = inner, False

    def latest(self):
        if self.down:
            raise ConnectionError("database unreachable")
        return self.inner.latest()

    def save(self, *args):
        return self.inner.save(*args)

    def history(self, limit=10):
        return self.inner.history(limit)


def test_store_outage_keeps_the_last_limits_read(tmp_path):
    store = FlakyStore(SqliteSettingsStore(str(tmp_path / "s.db")))
    live = LiveSettings(FILE_POLICY, store, ttl=0)
    live.change({"daily_budget_usd": 15, "per_transaction_cap_usd": 15, "approval_threshold_usd": 5},
                changed_by="human")
    assert live.policy()["daily_budget_usd"] == 15
    store.down = True
    # Not the file's looser 60: the tightened limit stays in force.
    assert live.policy()["daily_budget_usd"] == 15
    assert live.change({"daily_budget_usd": 10}, changed_by="human") == {
        "ok": False, "reason": "settings_store_unavailable"}


def test_store_outage_before_any_read_uses_the_file(tmp_path):
    store = FlakyStore(SqliteSettingsStore(str(tmp_path / "s.db")))
    store.down = True
    assert LiveSettings(FILE_POLICY, store).policy()["daily_budget_usd"] == 60


def test_change_needs_a_name(tmp_path):
    with make_gate(tmp_path) as gate:
        assert gate.change_settings({"daily_budget_usd": 5}, changed_by=" ")["reason"] == (
            "changed_by_required")


# --- the dashboard is the only way in ---------------------------------------------

@pytest.fixture
def dashboard(tmp_path):
    gate = make_gate(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, TOKEN))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", gate
    httpd.shutdown()
    httpd.server_close()
    gate.close()


def post(base, body, token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    return httpx.post(f"{base}/api/settings", content=json.dumps(body), headers=headers)


def test_dashboard_changes_settings_with_the_token(dashboard):
    base, gate = dashboard
    response = post(base, {"changes": {"daily_budget_usd": 40}})
    assert response.status_code == 200 and response.json()["reason"] == "saved"
    state = httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: TOKEN}).json()
    assert state["policy"]["daily_budget_usd"] == 40
    assert state["settings"]["version"] == 1
    assert state["settings"]["defaults"]["daily_budget_usd"] == 60
    assert state["settings"]["limits"]["daily_budget_usd"]["max"] == 400
    assert state["settings"]["history"][0]["changed_by"] == "human-dashboard"


@pytest.mark.parametrize("token", [None, "agent-token-abcdefghijklmn"])
def test_no_token_or_the_agent_token_cannot_change_settings(dashboard, token):
    base, gate = dashboard
    response = post(base, {"changes": {"daily_budget_usd": 400}, "confirm_loosening": True},
                    token=token)
    assert response.status_code == 401
    assert gate.policy["daily_budget_usd"] == 60


def test_dashboard_status_codes(dashboard):
    base, gate = dashboard
    assert post(base, {"changes": {"daily_budget_usd": -5}}).status_code == 400
    loosen = post(base, {"changes": {"daily_budget_usd": 100}})
    assert loosen.status_code == 409 and loosen.json()["loosened"] == ["daily_budget_usd"]
    assert post(base, {"changes": {"daily_budget_usd": 100}, "confirm_loosening": "yes"}
                ).status_code == 409  # only a real true confirms
    assert post(base, {"changes": {"daily_budget_usd": 100}, "confirm_loosening": True}
                ).status_code == 200
    assert post(base, ["not", "an", "object"]).status_code == 400
    assert gate.policy["daily_budget_usd"] == 100


def test_cross_site_settings_change_is_refused(dashboard):
    base, gate = dashboard
    response = httpx.post(
        f"{base}/api/settings",
        content=json.dumps({"changes": {"daily_budget_usd": 1}}),
        headers={"Content-Type": "application/json", TOKEN_HEADER: TOKEN,
                 "Origin": "https://evil.example"},
    )
    assert response.status_code == 403 and gate.policy["daily_budget_usd"] == 60


def test_state_works_without_any_change(tmp_path):
    with make_gate(tmp_path) as gate:
        state = build_state(gate)
    assert state["settings"]["version"] is None and state["settings"]["history"] == []


def test_the_agent_has_no_settings_tool():
    source = Path(mcp_server.__file__).read_text()
    assert "change_settings" not in source and "/api/settings" not in source


# --- S2: a hosted Postgres store (Neon) --------------------------------------------

import os
import sys
import time
import uuid
from contextlib import contextmanager

from leash.settings import PostgresSettingsStore, default_namespace, require_tls, store_from_env


@pytest.mark.parametrize("dsn, expected", [
    ("postgresql://u:p@ep-x.neon.tech/db", "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"),
    ("postgresql://u:p@ep-x.neon.tech/db?sslmode=verify-full",
     "postgresql://u:p@ep-x.neon.tech/db?sslmode=verify-full"),
    ("postgres://u:p@localhost/db", "postgres://u:p@localhost/db"),
])
def test_remote_database_urls_get_tls(dsn, expected):
    assert require_tls(dsn) == expected


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@ep-x.neon.tech/db?sslmode=disable",
    "postgresql://u:p@ep-x.neon.tech/db?sslmode=prefer",
    "mysql://u:p@host/db",
    "not a url",
])
def test_unsafe_or_wrong_database_urls_are_refused(dsn):
    with pytest.raises(ValueError):
        require_tls(dsn)


def test_namespace_is_explicit_then_public_host_then_local(monkeypatch):
    monkeypatch.delenv("LEASH_SETTINGS_NAMESPACE", raising=False)
    monkeypatch.delenv("LEASH_PUBLIC_HOST", raising=False)
    assert default_namespace() == "local"
    monkeypatch.setenv("LEASH_PUBLIC_HOST", "Leash.Tech, www.leash.tech")
    assert default_namespace() == "leash.tech"
    monkeypatch.setenv("LEASH_SETTINGS_NAMESPACE", "render")
    assert default_namespace() == "render"


def test_store_comes_from_the_environment(tmp_path, monkeypatch):
    assert isinstance(store_from_env(str(tmp_path / "a.db")), SqliteSettingsStore)
    monkeypatch.setenv("LEASH_SETTINGS_DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/db")
    monkeypatch.setenv("LEASH_SETTINGS_NAMESPACE", "render")
    store = store_from_env(str(tmp_path / "a.db"))
    assert isinstance(store, PostgresSettingsStore)
    assert store.namespace == "render" and store.dsn.endswith("sslmode=require")


def test_neon_env_pull_variables_are_used(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@ep-x-pooler.neon.tech/db")
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://u:p@ep-x.neon.tech/db")
    store = store_from_env(str(tmp_path / "a.db"))
    assert "-pooler" in store.dsn and "-pooler" not in store.migrate_dsn
    # Leash's own variable wins over Neon's generic one.
    monkeypatch.setenv("LEASH_SETTINGS_DATABASE_URL", "postgresql://u:p@other.neon.tech/db")
    assert "other.neon.tech" in store_from_env(str(tmp_path / "a.db")).dsn


def test_table_is_created_over_the_direct_url_once():
    seen = []
    pg = FakePg()

    def connect(dsn):
        seen.append("direct" if "-pooler" not in dsn else "pooled")
        return pg(dsn)

    store = PostgresSettingsStore("postgresql://u:p@ep-pooler.neon.tech/db", "local",
                                  connect=connect, migrate_dsn="postgresql://u:p@ep.neon.tech/db")
    store.latest()
    store.latest()
    # Direct once for the table, then one pooled connection kept for queries.
    assert seen == ["direct", "pooled"]
    assert sum(sql.startswith("CREATE TABLE") for sql, _ in pg.sql) == 1


@pytest.mark.parametrize("pasted", [
    '"postgresql://u:p@ep-x.neon.tech/db?sslmode=require"',
    "  'postgresql://u:p@ep-x.neon.tech/db?sslmode=require'  ",
    "psql 'postgresql://u:p@ep-x.neon.tech/db?sslmode=require'",
])
def test_common_paste_mistakes_are_tidied(tmp_path, monkeypatch, pasted):
    monkeypatch.setenv("DATABASE_URL", pasted)
    store = store_from_env(str(tmp_path / "a.db"))
    assert store.dsn == "postgresql://u:p@ep-x.neon.tech/db?sslmode=require"


def test_a_bad_url_names_the_variable_and_hides_the_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "https://secret-password@example.com")
    with pytest.raises(SystemExit) as stop:
        store_from_env(str(tmp_path / "a.db"))
    assert "DATABASE_URL" in str(stop.value) and "secret-password" not in str(stop.value)


def test_a_bad_database_url_stops_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASH_SETTINGS_DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/db?sslmode=disable")
    with pytest.raises(SystemExit):
        store_from_env(str(tmp_path / "a.db"))


def test_missing_driver_stops_startup_instead_of_losing_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASH_SETTINGS_DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/db")
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(SystemExit, match="postgres"):
        store_from_env(str(tmp_path / "a.db"))


class FakePg:
    """Records SQL; answers the queries PostgresSettingsStore makes."""

    def __init__(self, fail=False):
        self.sql, self.rows, self.fail = [], [], fail
        self.opened = self.close_calls = 0
        self.closed = False
        self.drop_next = False

    def __call__(self, dsn):
        if self.fail:
            raise ConnectionError("no route to host")
        self.opened += 1
        self.closed = False
        return self

    @contextmanager
    def transaction(self):
        yield

    def close(self):
        self.close_calls += 1
        self.closed = True

    def execute(self, sql, params=()):
        if self.fail or self.drop_next:
            self.drop_next = False
            raise ConnectionError("server closed the connection unexpectedly")
        sql = " ".join(sql.split())
        self.sql.append((sql, params))
        self._result = []
        if sql.startswith("INSERT"):
            self.rows.append((len(self.rows) + 1, *params))
            self._result = [(len(self.rows),)]
        elif sql.startswith("SELECT version, values_json"):
            mine = [r for r in self.rows if r[1] == params[0]]
            self._result = [(r[0], r[2]) for r in mine[-1:]]
        elif sql.startswith("SELECT version, changes_json"):
            mine = [r for r in self.rows if r[1] == params[0]][::-1][: params[1]]
            self._result = [(r[0], r[3], r[4], r[5]) for r in mine]
        return self

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


def test_postgres_store_keeps_each_copy_of_leash_apart():
    pg = FakePg()
    local = PostgresSettingsStore("postgresql://u:p@h.neon.tech/db", "local", connect=pg)
    render = PostgresSettingsStore("postgresql://u:p@h.neon.tech/db", "render", connect=pg)
    LiveSettings(FILE_POLICY, local).change({"daily_budget_usd": 30}, changed_by="human")
    assert LiveSettings(FILE_POLICY, local, ttl=0).policy()["daily_budget_usd"] == 30
    assert LiveSettings(FILE_POLICY, render, ttl=0).policy()["daily_budget_usd"] == 60
    assert local.history()[0]["changes"] == {"daily_budget_usd": {"from": 60, "to": 30}}
    assert render.history() == []
    # Every query is parameterised and scoped to the namespace.
    reads = [p for sql, p in pg.sql if sql.startswith("SELECT")]
    assert reads and all(p[0] in {"local", "render"} for p in reads)
    creates = [sql for sql, _ in pg.sql if sql.startswith("CREATE TABLE")]
    assert len(creates) == 2  # once per store object, not per query
    # Each store keeps one connection for its queries, plus one to set up the
    # table: 2 stores -> 4 connections, however many queries ran.
    assert pg.opened == 4


def test_postgres_store_reconnects_once_for_a_read_but_never_retries_a_write():
    pg = FakePg()
    store = PostgresSettingsStore("postgresql://u:p@h.neon.tech/db", "local", connect=pg)
    store.latest()
    pg.drop_next = True  # Neon suspended the idle database
    assert store.latest() is None
    pg.drop_next = True
    with pytest.raises(ConnectionError):
        store.save(CURRENT, {}, "human")
    assert not [sql for sql, _ in pg.sql if sql.startswith("INSERT")]
    store.close()
    assert pg.closed


class SlowStore(SqliteSettingsStore):
    """A network store: reads are counted and can be held back."""

    slow = True

    def __init__(self, path):
        super().__init__(path)
        self.reads, self.gate = 0, threading.Event()
        self.gate.set()

    def latest(self):
        self.reads += 1
        self.gate.wait(5)
        return super().latest()


def test_a_slow_store_never_makes_a_decision_wait(tmp_path):
    store = SlowStore(str(tmp_path / "s.db"))
    writer = LiveSettings(FILE_POLICY, SqliteSettingsStore(str(tmp_path / "s.db")))
    live = LiveSettings(FILE_POLICY, store, ttl=0)
    assert live.policy()["daily_budget_usd"] == 60       # first read waits
    writer.change({"daily_budget_usd": 30}, changed_by="human")
    store.gate.clear()                                    # the network is now slow
    started = time.monotonic()
    assert live.policy()["daily_budget_usd"] == 60       # stale copy, at once
    assert time.monotonic() - started < 0.5
    store.gate.set()
    for _ in range(50):                                   # the refresh lands
        if live.policy()["daily_budget_usd"] == 30:
            break
        time.sleep(0.02)
    assert live.policy()["daily_budget_usd"] == 30


def test_a_save_applies_at_once_without_another_read(tmp_path):
    store = SlowStore(str(tmp_path / "s.db"))
    live = LiveSettings(FILE_POLICY, store, ttl=60)
    live.policy()
    reads = store.reads
    assert live.change({"daily_budget_usd": 30}, changed_by="human")["ok"]
    assert store.reads == reads + 1                       # the check before saving
    assert live.policy()["daily_budget_usd"] == 30 and store.reads == reads + 1


def test_postgres_outage_keeps_limits_and_refuses_changes():
    pg = FakePg()
    store = PostgresSettingsStore("postgresql://u:p@h.neon.tech/db", "local", connect=pg)
    live = LiveSettings(FILE_POLICY, store, ttl=0)
    live.change({"daily_budget_usd": 30}, changed_by="human")
    pg.fail = True
    assert live.policy()["daily_budget_usd"] == 30
    assert live.change({"daily_budget_usd": 25}, changed_by="human")["reason"] == (
        "settings_store_unavailable")


def test_a_failed_write_is_reported_not_raised(tmp_path):
    class ReadOnly(SqliteSettingsStore):
        def save(self, *args):
            raise ConnectionError("read-only replica")

    live = LiveSettings(FILE_POLICY, ReadOnly(str(tmp_path / "s.db")))
    assert live.change({"daily_budget_usd": 30}, changed_by="human") == {
        "ok": False, "reason": "settings_store_unavailable"}


@pytest.mark.skipif(not os.getenv("LEASH_TEST_POSTGRES_URL"), reason="needs LEASH_TEST_POSTGRES_URL")
def test_real_postgres_round_trip():
    import psycopg

    namespace = f"pytest-{uuid.uuid4()}"
    store = PostgresSettingsStore(
        os.environ["LEASH_TEST_POSTGRES_URL"], namespace,
        migrate_dsn=os.getenv("LEASH_TEST_POSTGRES_URL_UNPOOLED") or None,
    )
    try:
        live = LiveSettings(FILE_POLICY, store, ttl=0)
        assert live.policy()["daily_budget_usd"] == 60
        assert live.change({"daily_budget_usd": 30}, changed_by="pytest")["version"] >= 1
        # A fresh object, as after a Render restart, still sees it.
        again = LiveSettings(
            FILE_POLICY, PostgresSettingsStore(store.dsn, namespace, migrate_dsn=store.migrate_dsn),
            ttl=0,
        )
        assert again.policy()["daily_budget_usd"] == 30
        assert again.store.history()[0]["changed_by"] == "pytest"
    finally:
        with psycopg.connect(store.migrate_dsn) as connection:
            connection.execute(f"DELETE FROM {store.TABLE} WHERE namespace = %s", (namespace,))
