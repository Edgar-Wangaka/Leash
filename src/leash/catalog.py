"""Channel3 product catalog: the source of truth for what things cost.

Search results are shown to the agent. Lookups are made by Leash itself, so
the gate can check an agent's stated price against the live catalog price
instead of taking the agent's word for it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


CHANNEL3_BASE_URL = "https://api.trychannel3.com"
CHANNEL3_SEARCH_URL = f"{CHANNEL3_BASE_URL}/v1/search"
# Product IDs come from the agent and go into a URL path, so only plain
# identifier characters are accepted.
PRODUCT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Channel3 keeps about one price a day per product; 30 days is the window
# the "is this price normal?" check compares against.
HISTORY_DAYS = 30

logger = logging.getLogger(__name__)


def sandbox_seller_for(domain: str, allowlist: list[str]) -> str:
    """Pick the PayPal sandbox account that stands in for a real retailer.

    Real retailers have no PayPal sandbox account, so each retailer domain is
    mapped, always the same way, onto one of the approved sandbox sellers.
    """
    digest = hashlib.sha256(domain.lower().encode()).digest()
    return allowlist[digest[0] % len(allowlist)]


def normalize_channel3_product(
    product: dict[str, Any], allowlist: list[str]
) -> dict[str, Any] | None:
    """Reduce a Channel3 product to the cheapest in-stock offer, or None."""
    offers = [
        offer
        for offer in product.get("offers") or []
        if offer.get("availability", "InStock") == "InStock"
        and isinstance((offer.get("price") or {}).get("price"), (int, float))
    ]
    if not offers:
        return None
    offer = min(offers, key=lambda item: item["price"]["price"])
    brands = product.get("brands") or []
    description = (product.get("description") or "").strip()
    return {
        "name": product.get("title", ""),
        "description": description[:400],
        "brand": brands[0].get("name") if brands else None,
        "sold_by": offer.get("domain", ""),
        "merchant_email": sandbox_seller_for(offer.get("domain", ""), allowlist),
        "amount_usd": f"{offer['price']['price']:.2f}",
        "channel3_product_id": product.get("id"),
        # The retailer's product page for that offer, for an agent to open.
        # Untrusted like the rest of the listing; only http(s) is kept.
        "url": _page_url(offer.get("url")),
    }


def _page_url(value: Any) -> str | None:
    if isinstance(value, str) and urlparse(value).scheme in {"http", "https"}:
        return value
    return None


def unavailable(error: str) -> dict[str, Any]:
    return {"status": "unavailable", "error": error}


def lookup_product(
    product_id: str,
    *,
    api_key: str,
    allowlist: list[str],
    http_client: httpx.Client | None = None,
    with_history: bool = True,
) -> dict[str, Any]:
    """Fetch one product from Channel3 and return its live price.

    With ``with_history`` the result also carries ``history``: the product's
    median price over the last 30 days (see price_history).

    Returns ``{"status": "ok", "price_usd", "name", "sold_by", ...}`` or
    ``{"status": "unavailable", "error"}``. It never raises: a failed lookup
    is a fact the policy rules handle, not a crash.
    """
    if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
        return unavailable("invalid_product_id")
    client = http_client or httpx.Client()
    try:
        response = client.get(
            f"{CHANNEL3_BASE_URL}/v1/products/{product_id}",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        if response.status_code == 404:
            return unavailable("product_not_found")
        response.raise_for_status()
        raw = response.json()
        product = normalize_channel3_product(raw, allowlist)
        # The variant this price is for, so a price watch follows the same one.
        selection = (raw.get("variants") or {}).get("selected") or None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
        logger.warning("Channel3 lookup failed: %s", error.__class__.__name__)
        return unavailable(error.__class__.__name__)
    finally:
        if http_client is None:
            client.close()
    if product is None:
        return unavailable("no_in_stock_offer")
    result = {
        "status": "ok",
        "product_id": product_id,
        "name": product["name"],
        "sold_by": product["sold_by"],
        "merchant_email": product["merchant_email"],
        "price_usd": product["amount_usd"],
    }
    if selection:
        result["selection"] = selection
    if product.get("url"):
        # The shop's offer page (a Channel3 affiliate link). Shown next to the
        # payment as "Buy on <shop>"; short-lived, so never reused later.
        result["url"] = product["url"]
    if with_history:
        result["history"] = price_history(
            product_id, api_key=api_key, http_client=http_client
        )
    return result


def price_history(
    product_id: str,
    *,
    api_key: str,
    http_client: httpx.Client | None = None,
    days: int = HISTORY_DAYS,
) -> dict[str, Any]:
    """The product's median USD price over the last ``days`` days.

    Returns ``{"status": "ok", "median_usd", "points", "days"}`` or
    ``{"status": "unavailable", "error"}``. Many products have no history
    yet (``points`` is then 0). Never raises.

    The median, not Channel3's mean: one day at a silly price moves a mean,
    and a seller raising the price for a few days is what this looks for.
    """
    if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
        return unavailable("invalid_product_id")
    client = http_client or httpx.Client()
    try:
        response = client.get(
            f"{CHANNEL3_BASE_URL}/v0/price-tracking/history/{product_id}",
            headers={"x-api-key": api_key},
            params={"days": days},
            timeout=10,
        )
        if response.status_code == 404:
            return unavailable("product_not_found")
        response.raise_for_status()
        points = response.json().get("history") or []
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        logger.warning("Channel3 price history failed: %s", error.__class__.__name__)
        return unavailable(error.__class__.__name__)
    finally:
        if http_client is None:
            client.close()
    prices = []
    for point in points if isinstance(points, list) else []:
        if not isinstance(point, dict) or point.get("currency", "USD") != "USD":
            continue
        try:
            price = Decimal(str(point.get("price")))
        except (InvalidOperation, ValueError):
            continue
        if price.is_finite() and price > 0:
            prices.append(price)
    return {
        "status": "ok",
        "median_usd": f"{median(prices):.2f}" if prices else None,
        "points": len(prices),
        "days": days,
    }


def price_lookup_from_env(allowlist: list[str]) -> Callable[[str], dict[str, Any]] | None:
    """Return a lookup function when CHANNEL3_API_KEY is set, else None."""
    api_key = os.getenv("CHANNEL3_API_KEY")
    if not api_key:
        return None
    return lambda product_id: lookup_product(
        product_id, api_key=api_key, allowlist=allowlist
    )
