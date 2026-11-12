from __future__ import annotations

import json

import httpx
import pytest

from leash.catalog import lookup_product
from leash.gate import PaymentGate
from leash.policy import PaymentRequest


SELLER = "sb-fwgx550980251@personal.example.com"
SELLERS = [SELLER]


class FakePayout:
    def __init__(self):
        self.calls = []

    def send_payout(self, *args):
        self.calls.append(args)
        return {"status": "success", "paypal_batch_id": "batch-1"}

    def close(self):
        pass


def catalog_says(price):
    return lambda product_id: {
        "status": "ok",
        "product_id": product_id,
        "name": "Birthday Bear",
        "sold_by": "lego.com",
        "price_usd": price,
    }


def request(amount, product_id="prod_1", key="r1"):
    return PaymentRequest(
        key, "agent-1", SELLER, amount, "Birthday Bear", "demo-legitimate-task", product_id
    )


def gate_with(tmp_path, payout, price_lookup, **policy_overrides):
    policy = {
        "per_transaction_cap_usd": 25,
        "daily_budget_usd": 60,
        "merchant_allowlist": SELLERS,
        "approval_threshold_usd": 10,
        "max_requests_per_minute": 100,
        "approved_task_ids": ["demo-legitimate-task"],
        "human_approval_expiry_hours": 24,
        **policy_overrides,
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return PaymentGate(
        db_path=str(tmp_path / "leash.db"),
        policy_path=path,
        payout_client=payout,
        risk_signal=lambda request, task: ("none", "none"),
        price_lookup=price_lookup,
    )


def test_amount_matching_catalog_price_is_paid(tmp_path):
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_says("4.99")) as gate:
        result = gate.process(request("4.99"))

    assert result["status"] == "paid"
    assert "Channel3 lists this product at $4.99 from lego.com." in result["explanation"]
    assert result["request"]["catalog"]["price_usd"] == "4.99"


def test_overstated_price_is_blocked_and_never_paid(tmp_path):
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_says("4.99")) as gate:
        result = gate.process(request("9.00"))
        approved = gate.approve("r1", "human-reviewer")

    assert result["verdict"] == "block"
    assert result["reasons"] == ["price_mismatch"]
    assert "Channel3 lists this product at $4.99 from lego.com, and this request asked for $9.00" in result["explanation"]
    assert approved["ok"] is False
    assert payout.calls == []


@pytest.mark.parametrize("amount,paid", [("5.23", True), ("5.25", False)])
def test_small_price_drift_is_tolerated_up_to_five_percent(tmp_path, amount, paid):
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_says("4.99")) as gate:
        result = gate.process(request(amount))

    assert (result["status"] == "paid") is paid


def test_paying_less_than_the_catalog_price_is_allowed(tmp_path):
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_says("9.00")) as gate:
        assert gate.process(request("4.00"))["status"] == "paid"


@pytest.mark.parametrize(
    "lookup",
    [
        lambda product_id: {"status": "unavailable", "error": "product_not_found"},
        lambda product_id: (_ for _ in ()).throw(RuntimeError("catalog down")),
    ],
)
def test_unverifiable_price_waits_for_a_human(tmp_path, lookup):
    payout = FakePayout()
    with gate_with(tmp_path, payout, lookup) as gate:
        held = gate.process(request("4.99"))
        approved = gate.approve("r1", "human-reviewer")

    assert held["verdict"] == "hold_for_human"
    assert held["reasons"] == ["price_unverified"]
    assert "Channel3 could not confirm the price of this product" in held["explanation"]
    assert approved["status"] == "paid"
    assert len(payout.calls) == 1


def test_no_product_id_waits_for_a_human_by_default(tmp_path):
    # Leaving out the product must not be a way around the price check.
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_says("1.00")) as gate:
        result = gate.process(request("8", product_id=None))

    assert result["verdict"] == "hold_for_human"
    assert result["reasons"] == ["price_unverified"]
    assert payout.calls == []


def test_policy_can_opt_out_of_requiring_a_catalog_product(tmp_path):
    payout = FakePayout()
    with gate_with(
        tmp_path, payout, catalog_says("1.00"), require_catalog_product=False
    ) as gate:
        assert gate.process(request("8", product_id=None))["status"] == "paid"


OTHER_SELLER = "sb-ze5vz53545847@personal.example.com"


def catalog_sold_by(seller, price="4.99"):
    return lambda product_id: {**catalog_says(price)(product_id), "merchant_email": seller}


def test_paying_a_different_seller_than_the_catalog_names_is_blocked(tmp_path):
    payout = FakePayout()
    with gate_with(
        tmp_path, payout, catalog_sold_by(OTHER_SELLER), merchant_allowlist=[SELLER, OTHER_SELLER]
    ) as gate:
        result = gate.process(request("4.99"))  # addressed to SELLER
        approved = gate.approve("r1", "human-reviewer")

    assert result["verdict"] == "block"
    assert result["reasons"] == ["payee_mismatch"]
    assert "different seller than the one lego.com lists" in result["explanation"]
    assert approved["ok"] is False
    assert payout.calls == []


def test_paying_the_seller_the_catalog_names_is_allowed(tmp_path):
    payout = FakePayout()
    with gate_with(tmp_path, payout, catalog_sold_by(SELLER.upper())) as gate:
        assert gate.process(request("4.99"))["status"] == "paid"


def test_price_check_is_off_when_no_catalog_is_configured(tmp_path):
    payout = FakePayout()
    with gate_with(tmp_path, payout, None, require_catalog_product=True) as gate:
        assert gate.process(request("8"))["status"] == "paid"


def channel3(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lookup_reads_the_cheapest_in_stock_price_from_channel3():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(
            200,
            json={
                "id": "prod_1",
                "title": "Birthday Bear",
                "offers": [
                    {"domain": "a.example", "price": {"price": 6.5}, "availability": "InStock"},
                    {"domain": "lego.com", "price": {"price": 4.99}, "availability": "InStock"},
                ],
            },
        )

    result = lookup_product(
        "prod_1", api_key="test-key", allowlist=SELLERS, http_client=channel3(handler),
        with_history=False,
    )

    assert result == {
        "status": "ok",
        "product_id": "prod_1",
        "name": "Birthday Bear",
        "sold_by": "lego.com",
        "merchant_email": SELLER,
        "price_usd": "4.99",
    }
    assert str(seen[0].url) == "https://api.trychannel3.com/v1/products/prod_1"
    assert seen[0].headers["x-api-key"] == "test-key"


@pytest.mark.parametrize("status_code,error", [(404, "product_not_found"), (500, "HTTPStatusError")])
def test_lookup_failures_are_reported_not_raised(status_code, error):
    result = lookup_product(
        "prod_1",
        api_key="test-key",
        allowlist=SELLERS,
        http_client=channel3(lambda http_request: httpx.Response(status_code, json={})),
    )

    assert result == {"status": "unavailable", "error": error}


@pytest.mark.parametrize("product_id", ["../v1/search", "a/b", "a?x=1", "", "x" * 65, None])
def test_unsafe_product_ids_never_reach_the_network(product_id):
    def explode(http_request):
        raise AssertionError("an unsafe product ID was sent to Channel3")

    result = lookup_product(
        product_id, api_key="test-key", allowlist=SELLERS, http_client=channel3(explode)
    )

    assert result == {"status": "unavailable", "error": "invalid_product_id"}
