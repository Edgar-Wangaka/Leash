"""Channel3 price history: a price far above the product's 30-day median waits.

A seller can raise a listing to match what a fooled agent asks for. The catalog
price check alone would then pass, because the agent's price matches the live
price. The 30-day median is what the product normally costs.
"""

from __future__ import annotations

import httpx
import pytest

from leash.catalog import lookup_product, price_history
from leash.policy import PaymentRequest, evaluate
from test_price_check import SELLER, SELLERS, FakePayout, channel3, gate_with, request


def history_body(prices, currency="USD"):
    # The shape the real API returned (2026-11): one point a day.
    return {
        "canonical_product_id": "prod_1",
        "history": [
            {"timestamp": f"2026-10-{day + 1:02d}T00:00:00Z", "price": price, "currency": currency}
            for day, price in enumerate(prices)
        ],
        "statistics": {"mean": 0, "std_dev": 0, "current_status": "typical"},
    }


# --- reading the history ------------------------------------------------------

def test_history_is_the_median_of_the_daily_prices():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(200, json=history_body([10, 10, 11, 10, 95, 10, 12]))

    result = price_history("prod_1", api_key="k", http_client=channel3(handler))

    assert result == {"status": "ok", "median_usd": "10.00", "points": 7, "days": 30}
    assert seen[0].url.path == "/v0/price-tracking/history/prod_1"
    assert seen[0].url.params["days"] == "30"
    assert seen[0].headers["x-api-key"] == "k"


def test_history_skips_other_currencies_and_junk_points():
    body = history_body([10, 12])
    body["history"] += [
        {"price": 500, "currency": "EUR"},
        {"price": "abc", "currency": "USD"},
        {"price": -3, "currency": "USD"},
        "not a point",
    ]
    result = price_history("prod_1", api_key="k", http_client=channel3(
        lambda r: httpx.Response(200, json=body)
    ))
    assert result["points"] == 2
    assert result["median_usd"] == "11.00"


def test_a_product_with_no_history_has_no_median():
    result = price_history("prod_1", api_key="k", http_client=channel3(
        lambda r: httpx.Response(200, json={"history": [], "statistics": None})
    ))
    assert result == {"status": "ok", "median_usd": None, "points": 0, "days": 30}


@pytest.mark.parametrize("answer", [
    httpx.Response(500), httpx.Response(200, text="not json"), httpx.Response(404),
])
def test_a_failed_history_is_unavailable_and_never_raises(answer):
    result = price_history("prod_1", api_key="k", http_client=channel3(lambda r: answer))
    assert result["status"] == "unavailable"


def test_a_bad_product_id_never_reaches_the_network():
    def handler(r):
        raise AssertionError("network call made")
    assert price_history("../etc", api_key="k", http_client=channel3(handler))["status"] == "unavailable"


def test_lookup_carries_the_history_and_survives_its_failure():
    def handler(http_request):
        if http_request.url.path.startswith("/v0/price-tracking/history/"):
            return httpx.Response(503)
        return httpx.Response(200, json={
            "id": "prod_1", "title": "Bear",
            "offers": [{"domain": "lego.com", "price": {"price": 9.0}, "availability": "InStock"}],
        })

    result = lookup_product("prod_1", api_key="k", allowlist=SELLERS, http_client=channel3(handler))

    assert result["status"] == "ok" and result["price_usd"] == "9.00"
    assert result["history"]["status"] == "unavailable"


# --- the rule -----------------------------------------------------------------

POLICY = {
    "per_transaction_cap_usd": 100,
    "daily_budget_usd": 250,
    "merchant_allowlist": SELLERS,
    "approval_threshold_usd": 40,
    "max_requests_per_minute": 100,
    "approved_task_ids": ["demo-legitimate-task"],
}


def catalog(price, median=None, points=30):
    entry = {"status": "ok", "price_usd": price, "merchant_email": SELLER, "sold_by": "lego.com"}
    if median is not None:
        entry["history"] = {"status": "ok", "median_usd": median, "points": points, "days": 30}
    return entry


def verdict(amount, cat, **policy):
    decision = evaluate(request(amount), {
        "policy": {**POLICY, **policy},
        "catalog": cat,
        "approved_task_ids": ["demo-legitimate-task"],
    })
    return decision.verdict, decision.reasons


def test_a_price_far_above_its_median_waits_for_a_human():
    assert verdict("30.00", catalog("30.00", median="20.00")) == (
        "hold_for_human", ["price_spike"],
    )


def test_up_to_the_allowed_rise_is_paid():
    assert verdict("25.00", catalog("25.00", median="20.00")) == ("allow", [])


def test_the_allowed_rise_comes_from_the_policy():
    assert verdict("25.00", catalog("25.00", median="20.00"), price_spike_pct=10)[1] == ["price_spike"]


@pytest.mark.parametrize("cat", [
    catalog("30.00"),                                # no history at all
    catalog("30.00", median="20.00", points=3),      # too few days to say what is normal
    {**catalog("30.00"), "history": {"status": "unavailable", "error": "HTTPStatusError"}},
    {**catalog("30.00"), "history": {"status": "ok", "median_usd": None, "points": 0, "days": 30}},
])
def test_without_usable_history_the_rule_stays_quiet(cat):
    assert verdict("30.00", cat) == ("allow", [])


def test_a_hard_rule_still_blocks_alongside_a_spike():
    result = verdict("150.00", catalog("150.00", median="20.00"))
    assert result[0] == "block"
    assert "cap" in result[1] and "price_spike" in result[1]


# --- end to end through the gate ----------------------------------------------

def test_the_gate_holds_a_spiked_price_and_says_why(tmp_path):
    payout = FakePayout()

    def lookup(product_id):
        return {
            "status": "ok", "product_id": product_id, "name": "Birthday Bear",
            "sold_by": "lego.com", "merchant_email": SELLER, "price_usd": "9.00",
            "history": {"status": "ok", "median_usd": "6.00", "points": 30, "days": 30},
        }

    with gate_with(tmp_path, payout, lookup) as gate:
        result = gate.process(request("9.00"))

    assert result["verdict"] == "hold_for_human"
    assert result["status"] == "pending"
    assert payout.calls == []
    assert "more than 25% above this product's usual price ($6.00" in result["explanation"]
