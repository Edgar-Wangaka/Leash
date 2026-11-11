"""The OpenAPI spec, the Postman collection and the dashboard stay in step."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from leash import dashboard, events

ROOT = Path(__file__).parents[1]
SPEC = yaml.safe_load((ROOT / "docs/openapi.yaml").read_text())
COLLECTION = json.loads((ROOT / "postman/leash.postman_collection.json").read_text())
ENVIRONMENTS = sorted((ROOT / "postman").glob("*.postman_environment.json"))


def requests(items=COLLECTION["item"]):
    for item in items:
        if "item" in item:
            yield from requests(item["item"])
        else:
            yield item


def handler_routes():
    """Paths the dashboard handler answers, read from its source."""
    source = Path(dashboard.__file__).read_text()
    return set(re.findall(r'"(/api/[a-z]+|/healthz)"', source))


# --- spec ---------------------------------------------------------------------

def test_spec_is_openapi_3_1():
    assert SPEC["openapi"] == "3.1.0"
    assert SPEC["components"]["securitySchemes"]["dashboardToken"]["name"] == dashboard.TOKEN_HEADER


def test_spec_documents_every_route_the_dashboard_serves():
    spec_paths = set(SPEC["paths"])
    assert handler_routes() <= spec_paths
    assert "/demo/listing/{slug}" in spec_paths


def test_spec_simulate_scenarios_match_the_code():
    body = SPEC["paths"]["/api/simulate"]["post"]["requestBody"]["content"]["application/json"]
    assert set(body["schema"]["properties"]["scenario"]["enum"]) == set(dashboard.SCENARIOS)


def test_spec_decision_schema_matches_the_public_record():
    record = {
        "request_id": "r", "agent_id": "a", "merchant_email": "m", "amount_cents": 800,
        "request": {"reason": "x", "catalog": None, "product_id": None}, "verdict": "allow",
        "reasons": [], "status": "paid", "explanation": "", "risk_signal": "none",
        "approved_by": None, "payout": None, "error_name": None, "created_at": "t",
    }
    documented = set(SPEC["components"]["schemas"]["Decision"]["properties"])
    assert set(events.public_record(record)) == documented


def test_only_health_the_demo_page_and_the_signed_webhook_skip_the_token():
    open_routes = {
        path for path, operations in SPEC["paths"].items()
        for operation in operations.values() if operation.get("security") == []
    }
    # The Channel3 webhook has no token but must carry a valid Svix signature
    # (tests/test_price_watch.py checks that it refuses one without).
    assert open_routes == {"/healthz", "/demo/listing/{slug}", "/webhooks/channel3"}


def test_every_internal_ref_resolves():
    refs = re.findall(r"\$ref['\"]?:\s*['\"]?#/([^'\"\s}]+)", (ROOT / "docs/openapi.yaml").read_text())
    for ref in refs:
        node = SPEC
        for part in ref.split("/"):
            node = node[part]
        assert node, ref


# --- collection ---------------------------------------------------------------

def test_every_request_asserts_something():
    for item in requests():
        tests = [e for e in item.get("event", []) if e["listen"] == "test"]
        assert tests and "pm.test(" in "\n".join(tests[0]["script"]["exec"]), item["name"]


def test_every_request_targets_a_documented_route():
    templates = [re.sub(r"\{[^}]+\}", "[^/]+", path) for path in SPEC["paths"]]
    for item in requests():
        path = "/" + "/".join(item["request"]["url"]["path"])
        assert any(re.fullmatch(t, path) for t in templates), (item["name"], path)


def test_attack_folder_covers_the_dashboard_attack_tests():
    attacks = next(f for f in COLLECTION["item"] if f["name"] == "Attacks")
    names = " ".join(item["name"].lower() for item in attacks["item"])
    for needle in ("without a token", "wrong token", "cross-site", "foreign host", "attack"):
        assert needle in names


def test_run_stays_under_the_rate_limit():
    simulates = [i for i in requests() if i["request"]["url"]["path"] == ["api", "simulate"]
                 and json.loads(i["request"]["body"]["raw"])["scenario"] in dashboard.SCENARIOS]
    policy = json.loads((ROOT / "config/policy.json").read_text())
    assert len(simulates) < policy["max_requests_per_minute"]


def test_no_token_is_committed():
    for path in ENVIRONMENTS:
        values = {v["key"]: v for v in json.loads(path.read_text())["values"]}
        assert values["dashboardToken"]["value"] == ""
        assert values["dashboardToken"]["type"] == "secret"
    raw = (ROOT / "postman/leash.postman_collection.json").read_text()
    assert "{{dashboardToken}}" in raw
