"""Channel3 price watch: held products are watched; signed webhooks mark changes.

Payload and signature shapes follow Channel3's webhook docs (Svix / Standard
Webhooks); the subscription response shape is the one the real API returned
(2026-11): ``subscription_id``, not ``id``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import httpx
import pytest

from leash import events, price_watch, sinks
from leash.dashboard import TOKEN_HEADER, make_handler
from leash.gate import PaymentGate
from leash.policy import PaymentRequest
from leash.price_watch import Channel3Watcher, verify_signature, watch_sink


SECRET = "whsec_" + base64.b64encode(b"a-test-signing-key-32-bytes-long").decode()
SELLER = "sb-fwgx550980251@personal.example.com"
TOKEN = "test-token-123"


def sign(body: bytes, secret=SECRET, msg_id="msg_1", ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest())
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig.decode()}"}


def price_event(event_id="evt_1", product="prod_1", domain="lego.com", was=20.0, now=26.0,
                when=None, kind="offer.price.changed"):
    when = when or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if kind == "offer.price.changed":
        previous = {"price": was, "compare_at_price": None, "currency": "USD"}
        current = {"price": now, "compare_at_price": None, "currency": "USD"}
    else:
        previous, current = "InStock", "OutOfStock"
    return {
        "id": event_id, "timestamp": when, "type": kind,
        "data": {
            "subscription_id": "01SUB",
            "subject": {"product_id": product, "domain": domain,
                        "locale": {"language": "en", "country": "US", "currency": "USD"},
                        "selection": None},
            "previous": previous, "current": current,
        },
    }


# --- signature ----------------------------------------------------------------

def test_a_correct_signature_passes():
    body = b'{"a":1}'
    assert verify_signature(SECRET, sign(body), body)


def test_any_one_of_several_signatures_may_match():
    body = b'{"a":1}'
    headers = sign(body)
    headers["svix-signature"] = "v1,AAAA " + headers["svix-signature"]
    assert verify_signature(SECRET, headers, body)


@pytest.mark.parametrize("tamper", ["body", "secret", "old", "future", "missing", "bad_secret"])
def test_a_wrong_signature_fails(tamper):
    body = b'{"a":1}'
    headers = sign(body)
    secret = SECRET
    if tamper == "body":
        body = b'{"a":2}'
    elif tamper == "secret":
        secret = "whsec_" + base64.b64encode(b"another-key").decode()
    elif tamper == "old":
        headers = sign(body, ts=int(time.time()) - 600)
    elif tamper == "future":
        headers = sign(body, ts=int(time.time()) + 600)
    elif tamper == "missing":
        del headers["svix-signature"]
    elif tamper == "bad_secret":
        secret = "whsec_not base64!!"
    assert not verify_signature(secret, headers, body)


# --- subscribing --------------------------------------------------------------

def channel3(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_watch_subscribes_to_price_and_stock_at_the_priced_seller():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"subscription_id": "01ABC", "status": "active"})

    watcher = Channel3Watcher("k", http_client=channel3(handler))
    assert watcher.watch("prod_1", "lego.com", [{"name": "Size", "label": "L"}]) == "01ABC"
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/product-subscriptions"
    assert seen[0].headers["x-api-key"] == "k"
    assert body == {
        "product_id": "prod_1",
        "events": ["offer.price.changed", "offer.availability.changed"],
        "domains": ["lego.com"],
        "selection": [{"name": "Size", "label": "L"}],
    }


def test_watch_never_raises_and_rejects_bad_ids():
    def boom(request):
        raise httpx.ConnectError("down")

    watcher = Channel3Watcher("k", http_client=channel3(boom))
    assert watcher.watch("prod_1") is None
    assert watcher.watch("../x") is None
    assert watcher.unwatch("01ABC") is False


def test_unwatch_deletes_the_subscription():
    seen = []
    watcher = Channel3Watcher("k", http_client=channel3(
        lambda r: (seen.append(r), httpx.Response(200, json={"status": "cancelled"}))[1]
    ))
    assert watcher.unwatch("01ABC")
    assert seen[0].method == "DELETE" and seen[0].url.path == "/v1/product-subscriptions/01ABC"


class RecordingWatcher:
    def __init__(self):
        self.watched, self.unwatched = [], []

    def watch(self, *args):
        self.watched.append(args)
        return "01SUB"

    def unwatch(self, subscription_id):
        self.unwatched.append(subscription_id)
        return True


def held_record(catalog):
    return {"product_id": "prod_1", "catalog": catalog}


def test_the_sink_watches_only_held_priced_products():
    watcher = RecordingWatcher()
    sink = watch_sink(watcher)
    ok = {"status": "ok", "sold_by": "lego.com", "price_usd": "9.00"}
    sink("decision.held", held_record(ok))
    sink("decision.created", held_record(ok))                     # paid at once
    sink("decision.held", held_record({"status": "unavailable"}))  # nothing priced
    sink("decision.held", {"product_id": None, "catalog": ok})
    assert watcher.watched == [("prod_1", "lego.com", None)]


def test_the_watch_is_installed_only_with_key_and_secret(monkeypatch):
    monkeypatch.setenv("CHANNEL3_API_KEY", "k")
    monkeypatch.delenv("CHANNEL3_WEBHOOK_SECRET", raising=False)
    assert "channel3_price_watch" not in [name for name, _ in sinks.from_env()]
    monkeypatch.setenv("CHANNEL3_WEBHOOK_SECRET", SECRET)
    assert "channel3_price_watch" in [name for name, _ in sinks.from_env()]


# --- the gate -----------------------------------------------------------------

def make_gate(tmp_path, now=None):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "per_transaction_cap_usd": 100, "daily_budget_usd": 250,
        "merchant_allowlist": [SELLER], "approval_threshold_usd": 10,
        "max_requests_per_minute": 100, "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
    }))
    lookup = lambda product_id: {
        "status": "ok", "product_id": product_id, "name": "Bear", "sold_by": "lego.com",
        "merchant_email": SELLER, "price_usd": "20.00",
    }
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"), policy_path=policy, payout_client=None,
        risk_signal=lambda request, task: ("none", "none"), price_lookup=lookup,
        **({"now": now} if now else {}),
    )


def hold(gate, key="held-1", product="prod_1"):
    return gate.process(PaymentRequest(
        key, "agent-1", SELLER, "20.00", "Bear", "demo-legitimate-task", product
    ))


def test_a_price_event_is_stored_once_and_names_the_waiting_requests(tmp_path):
    with make_gate(tmp_path) as gate:
        assert hold(gate)["status"] == "pending"
        first = gate.record_price_event(price_event())
        again = gate.record_price_event(price_event())     # Svix redelivery
    assert first == {"recorded": True, "pending_request_ids": ["held-1"]}
    assert again["recorded"] is False


@pytest.mark.parametrize("event", [
    {}, {"type": "offer.created", "id": "e", "data": {"subject": {"product_id": "p"}}},
    {"type": "offer.price.changed", "data": {"subject": {"product_id": "p"}}},  # no id
])
def test_other_or_broken_events_are_ignored(tmp_path, event):
    with make_gate(tmp_path) as gate:
        assert gate.record_price_event(event)["ignored"] is True


def test_alerts_show_the_change_since_the_request_at_its_seller(tmp_path):
    with make_gate(tmp_path) as gate:
        hold(gate)
        before = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        gate.record_price_event(price_event("old", when=before, was=10, now=11))
        gate.record_price_event(price_event("other-seller", domain="amazon.com", was=20, now=99))
        gate.record_price_event(price_event("e1", was=20, now=24))
        gate.record_price_event(price_event("e2", was=24, now=26))
        gate.record_price_event(price_event("e3", kind="offer.availability.changed"))
        alerts = gate.price_alerts()

    price = alerts["held-1"]["price"]
    assert price["previous"]["price"] == 20 and price["current"]["price"] == 26
    assert price["changes"] == 2 and price["domain"] == "lego.com"
    assert alerts["held-1"]["availability"]["current"] == "OutOfStock"


def test_a_resolved_hold_has_no_alert_and_no_longer_counts_as_waiting(tmp_path):
    with make_gate(tmp_path) as gate:
        hold(gate)
        gate.record_price_event(price_event("e1"))
        gate.deny("held-1", "human", "no")
        assert gate.price_alerts() == {}
        assert gate.has_pending_hold_for("prod_1") is False


def test_a_price_event_changes_no_decision(tmp_path):
    with make_gate(tmp_path) as gate:
        hold(gate)
        before = gate.get("held-1")
        gate.record_price_event(price_event("e1", was=20, now=5))
        after = gate.get("held-1")
    assert (before["status"], before["verdict"], before["reasons"]) == (
        after["status"], after["verdict"], after["reasons"]
    )


# --- the webhook endpoint -----------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANNEL3_WEBHOOK_SECRET", SECRET)
    watcher = RecordingWatcher()
    monkeypatch.setattr(price_watch, "watcher_from_env", lambda: watcher)
    gate = make_gate(tmp_path)
    hold(gate)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate, TOKEN))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", gate, watcher
    httpd.shutdown()
    httpd.server_close()
    gate.close()


def post_event(base, event, headers=None):
    body = json.dumps(event).encode()
    return httpx.post(f"{base}/webhooks/channel3", content=body, headers={
        "Content-Type": "application/json", **(sign(body) if headers is None else headers),
    })


def test_a_signed_webhook_needs_no_token_and_shows_on_the_dashboard(server):
    base, gate, _ = server
    response = post_event(base, price_event("e1", was=20, now=26))
    assert response.status_code == 200 and response.json() == {"ok": True, "recorded": True}

    state = httpx.get(f"{base}/api/state", headers={TOKEN_HEADER: TOKEN}).json()
    assert state["price_alerts"]["held-1"]["price"]["current"]["price"] == 26


def test_an_unsigned_or_forged_webhook_is_refused(server):
    base, gate, _ = server
    assert post_event(base, price_event(), headers={}).status_code == 401
    forged = sign(b"something else")
    assert post_event(base, price_event(), headers=forged).status_code == 401
    assert gate.price_alerts() == {}


def test_without_a_secret_there_is_no_webhook(server, monkeypatch):
    base, _, _ = server
    monkeypatch.delenv("CHANNEL3_WEBHOOK_SECRET")
    assert post_event(base, price_event()).status_code == 404


def test_the_webhook_keeps_the_host_check(server):
    base, _, _ = server
    body = json.dumps(price_event()).encode()
    response = httpx.post(f"{base}/webhooks/channel3", content=body, headers={
        "Content-Type": "application/json", "Host": "evil.example", **sign(body),
    })
    assert response.status_code == 403


def test_an_event_nobody_waits_for_stops_the_watch(server):
    base, gate, watcher = server
    gate.deny("held-1", "human", "no")
    assert post_event(base, price_event("e9")).status_code == 200
    deadline = time.time() + 2
    while not watcher.unwatched and time.time() < deadline:
        time.sleep(0.02)
    assert watcher.unwatched == ["01SUB"]


def test_an_event_for_a_waiting_request_keeps_the_watch(server):
    base, _, watcher = server
    assert post_event(base, price_event("e10")).status_code == 200
    time.sleep(0.1)
    assert watcher.unwatched == []
