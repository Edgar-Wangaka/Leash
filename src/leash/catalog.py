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
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


CHANNEL3_BASE_URL = "https://api.trychannel3.com"
CHANNEL3_SEARCH_URL = f"{CHANNEL3_BASE_URL}/v1/search"
# Product IDs come from the agent and go into a URL path, so only plain
# identifier characters are accepted.
PRODUCT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
) -> dict[str, Any]:
    """Fetch one product from Channel3 and return its live price.

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
        product = normalize_channel3_product(response.json(), allowlist)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        logger.warning("Channel3 lookup failed: %s", error.__class__.__name__)
        return unavailable(error.__class__.__name__)
    finally:
        if http_client is None:
            client.close()
    if product is None:
        return unavailable("no_in_stock_offer")
    return {
        "status": "ok",
        "product_id": product_id,
        "name": product["name"],
        "sold_by": product["sold_by"],
        "merchant_email": product["merchant_email"],
        "price_usd": product["amount_usd"],
    }


def price_lookup_from_env(allowlist: list[str]) -> Callable[[str], dict[str, Any]] | None:
    """Return a lookup function when CHANNEL3_API_KEY is set, else None."""
    api_key = os.getenv("CHANNEL3_API_KEY")
    if not api_key:
        return None
    return lambda product_id: lookup_product(
        product_id, api_key=api_key, allowlist=allowlist
    )
