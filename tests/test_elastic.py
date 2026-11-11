"""Elastic gets every event as an idempotent audit-log document."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from leash import events, sinks
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


SELLER = "sb-fwgx550980251@personal.example.com"
URL = "https://es.example.com:9243"
ROOT = Path(__file__).parents[1]


class FakePayout:
    def send_payout(self, *args):
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


def make_gate(tmp_path):
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
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=policy,
        payout_client=FakePayout(),
        risk_signal=lambda request, task: ("none", "none"),
    )


def req(key, amount="8.00", email=SELLER):
    return PaymentRequest(key, "agent-1", email, amount, "gift", "demo-legitimate-task")


@pytest.fixture
def indexed():
    docs = {}
    calls = []

    def handler(request):
        calls.append(request)
        docs[request.url.path] = json.loads(request.content)
        assert b"%3A" in request.url.raw_path  # the id is escaped on the wire
        return httpx.Response(201, json={"result": "created"})

    sink = sinks.ElasticSink(
        URL, "key-123",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    events.register(sink)
    return docs, calls, sink


def test_every_event_is_indexed_with_a_stable_id(tmp_path, indexed):
    docs, calls, _ = indexed
    gate = make_gate(tmp_path)
    gate.process(req("h-1", amount="15.00"))
    events.drain()
    gate.approve("h-1", "human")
    events.drain()

    assert sorted(docs) == [
        "/leash-decisions/_doc/h-1:decision.approved",
        "/leash-decisions/_doc/h-1:decision.held",
        "/leash-decisions/_doc/h-1:decision.paid",
    ]
    assert all(call.method == "PUT" for call in calls)
    assert all(call.headers["authorization"] == "ApiKey key-123" for call in calls)


def test_a_retried_event_overwrites_instead_of_duplicating(tmp_path, indexed):
    docs, calls, sink = indexed
    record = make_gate(tmp_path).process(req("h-1", amount="15.00"))
    events.drain()
    sink("decision.held", events.public_record(record))  # a retry

    assert len(calls) == 2
    assert len({call.url.path for call in calls}) == 1


def test_blocked_attack_document_has_numbers_for_kibana(tmp_path, indexed):
    docs, _, _ = indexed
    make_gate(tmp_path).process(req("b-1", amount="500.00", email="attacker@example.com"))
    events.drain()

    [doc] = docs.values()
    assert doc["@timestamp"] == "2026-11-11T00:00:00+00:00"
    assert doc["event"] == "decision.blocked"
    assert doc["amount_usd"] == 500.0
    assert doc["blocked_amount_usd"] == 500.0
    assert doc["is_attack"] is True
    assert "allowlist" in doc["attack_reasons"]
    assert isinstance(doc["reasons"], list)


def test_document_fields_match_the_index_template(tmp_path, indexed):
    docs, _, _ = indexed
    make_gate(tmp_path).process(req("h-1", amount="15.00"))
    events.drain()

    template = json.loads((ROOT / "deploy/elastic/index-template.json").read_text())
    mapped = set(template["template"]["mappings"]["properties"])
    [doc] = docs.values()
    assert set(doc) == mapped  # the mapping is strict, so they must agree


def test_elastic_outage_does_not_change_the_decision(tmp_path):
    events.register(sinks.ElasticSink(
        URL, "k", http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(503)
        )),
    ))
    record = make_gate(tmp_path).process(req("a-1"))
    events.drain()
    assert record["status"] == "paid"


def test_cloud_id_decodes_to_the_es_url():
    encoded = base64.b64encode(b"us-east-1.aws.found.io:443$abc123$kib456").decode()
    assert sinks.elastic_url_from_cloud_id(f"leash:{encoded}") == "https://abc123.us-east-1.aws.found.io"


def test_from_env_needs_a_url_and_a_key(monkeypatch):
    for name in ("ZAPIER_HOOK_URL", "ELASTIC_URL", "ELASTIC_CLOUD_ID", "ELASTIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ELASTIC_URL", URL)
    assert sinks.from_env() == []

    monkeypatch.setenv("ELASTIC_API_KEY", "k")
    [(name, sink)] = sinks.from_env()
    assert name == "elastic" and sink.base_url == URL


def test_bad_cloud_id_is_logged_not_raised(monkeypatch):
    monkeypatch.delenv("ELASTIC_URL", raising=False)
    monkeypatch.setenv("ELASTIC_CLOUD_ID", "not-base64!!")
    monkeypatch.setenv("ELASTIC_API_KEY", "k")
    assert sinks.from_env() == []


def test_kibana_export_is_valid_ndjson_with_a_data_view():
    lines = (ROOT / "deploy/kibana/leash.ndjson").read_text().splitlines()
    objects = [json.loads(line) for line in lines]
    assert {"index-pattern", "search"} <= {obj["type"] for obj in objects}
