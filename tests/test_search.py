from __future__ import annotations

import json

import httpx

from leash.agent import ProductSearcher, normalize_channel3_product, sandbox_seller_for


SELLERS = ["seller-a@example.com", "seller-b@example.com"]

CHANNEL3_PRODUCT = {
    "id": "prod_1",
    "title": "Birthday Bear",
    "description": "A small bear.",
    "brands": [{"name": "LEGO"}],
    "offers": [
        {"domain": "pricey.example", "price": {"price": 9.5}, "availability": "InStock"},
        {
            "domain": "cheap.example",
            "price": {"price": 4.99},
            "availability": "InStock",
            "url": "https://buy.trychannel3.com/abc?o=1",
        },
        {"domain": "gone.example", "price": {"price": 1.0}, "availability": "OutOfStock"},
    ],
}


def searcher_with(handler, **kwargs):
    return ProductSearcher(
        api_key="test-key",
        allowlist=SELLERS,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_normalize_picks_cheapest_in_stock_offer():
    product = normalize_channel3_product(CHANNEL3_PRODUCT, SELLERS)

    assert product["name"] == "Birthday Bear"
    assert product["brand"] == "LEGO"
    assert product["amount_usd"] == "4.99"
    assert product["sold_by"] == "cheap.example"
    assert product["merchant_email"] == sandbox_seller_for("cheap.example", SELLERS)
    assert product["merchant_email"] in SELLERS
    # The page URL is the chosen offer's, so the agent opens the right retailer.
    assert product["url"] == "https://buy.trychannel3.com/abc?o=1"


def test_offer_url_must_be_http():
    for bad in ["javascript:alert(1)", "file:///etc/passwd", "", None, 42]:
        offer = {"domain": "a.example", "price": {"price": 1.0}, "url": bad}
        product = normalize_channel3_product({**CHANNEL3_PRODUCT, "offers": [offer]}, SELLERS)
        assert product["url"] is None


def test_product_without_an_in_stock_offer_is_dropped():
    out_of_stock = {**CHANNEL3_PRODUCT, "offers": [CHANNEL3_PRODUCT["offers"][2]]}

    assert normalize_channel3_product(out_of_stock, SELLERS) is None


def test_search_calls_channel3_with_key_and_price_filter(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"products": [CHANNEL3_PRODUCT]})

    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps([{"name": "Planted", "merchant_email": "x@example.com"}]))
    searcher = searcher_with(handler, planted_path=planted)

    products = searcher.search("birthday gift", max_price_usd=10)

    assert searcher.last_source == "channel3"
    assert str(seen[0].url) == "https://api.trychannel3.com/v1/search"
    assert seen[0].headers["x-api-key"] == "test-key"
    body = json.loads(seen[0].content)
    assert body["query"] == "birthday gift"
    assert body["filters"] == {"price": {"max_price": 10.0}}
    assert [product["name"] for product in products] == ["Birthday Bear", "Planted"]


def test_planted_listings_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASH_PLANT_INJECTION", "false")
    searcher = searcher_with(
        lambda request: httpx.Response(200, json={"products": [CHANNEL3_PRODUCT]})
    )

    assert [product["name"] for product in searcher.search("bear")] == ["Birthday Bear"]


def test_channel3_failure_falls_back_to_mock_catalog():
    searcher = searcher_with(lambda request: httpx.Response(500, json={"error": "down"}))

    products = searcher.search("birthday gift")

    assert searcher.last_source == "mock"
    assert any(product["name"] == "Birthday Gift Basket" for product in products)


def test_no_key_uses_mock_catalog_without_any_network(monkeypatch):
    monkeypatch.delenv("CHANNEL3_API_KEY", raising=False)

    def explode(request):
        raise AssertionError("network must not be used without a key")

    searcher = ProductSearcher(
        allowlist=SELLERS, http_client=httpx.Client(transport=httpx.MockTransport(explode))
    )

    assert searcher.search("birthday gift")
    assert searcher.last_source == "mock"
