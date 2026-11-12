"""Channel3 price watch: tell the human when a held product's price moves.

When a request is held for a human, Leash subscribes to that product's offer
at the seller it was priced from (Channel3 Product Subscriptions). Channel3
re-checks the product daily and, when the price or stock changes, POSTs a
signed webhook to the dashboard (`/webhooks/channel3`). The held request then
shows what changed since the human was asked.

Nothing here can approve, block or pay. A price change is shown, not acted on.

Needs CHANNEL3_API_KEY and CHANNEL3_WEBHOOK_SECRET (the endpoint's `whsec_`
signing secret from the Channel3 dashboard's webhook portal). Without the
secret, nothing is subscribed: there would be nowhere to hear back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Callable, Mapping

import httpx

from .catalog import CHANNEL3_BASE_URL, PRODUCT_ID_PATTERN
from .events import SINK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

SUBSCRIPTIONS_URL = f"{CHANNEL3_BASE_URL}/v1/product-subscriptions"
WATCHED_EVENTS = ["offer.price.changed", "offer.availability.changed"]
WEBHOOK_PATH = "/webhooks/channel3"
# Svix (Standard Webhooks): reject messages older or newer than this.
SIGNATURE_TOLERANCE_SECONDS = 5 * 60
MAX_WEBHOOK_BYTES = 64_000


class Channel3Watcher:
    """Creates and cancels Channel3 product subscriptions. Never raises."""

    def __init__(self, api_key: str, http_client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._http = http_client

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"x-api-key": self._api_key}
        if self._http is not None:
            return self._http.request(method, url, headers=headers,
                                      timeout=SINK_TIMEOUT_SECONDS, **kwargs)
        with httpx.Client() as client:
            return client.request(method, url, headers=headers,
                                  timeout=SINK_TIMEOUT_SECONDS, **kwargs)

    def watch(
        self, product_id: str, domain: str | None = None,
        selection: list[dict[str, str]] | None = None,
    ) -> str | None:
        """Subscribe to price and stock changes; returns the subscription ID.

        Channel3 returns the existing subscription for an identical request,
        so watching the same product twice costs nothing.
        """
        if not isinstance(product_id, str) or not PRODUCT_ID_PATTERN.match(product_id):
            return None
        body: dict[str, Any] = {"product_id": product_id, "events": WATCHED_EVENTS}
        if domain:
            body["domains"] = [domain]
        if selection:
            body["selection"] = selection
        try:
            response = self._request("POST", SUBSCRIPTIONS_URL, json=body)
            response.raise_for_status()
            return response.json().get("subscription_id")
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            logger.warning("Channel3 watch failed: %s", error.__class__.__name__)
            return None

    def unwatch(self, subscription_id: str) -> bool:
        if not isinstance(subscription_id, str) or not PRODUCT_ID_PATTERN.match(subscription_id):
            return False
        try:
            response = self._request("DELETE", f"{SUBSCRIPTIONS_URL}/{subscription_id}")
            return response.status_code in {200, 204, 404}
        except httpx.HTTPError as error:
            logger.warning("Channel3 unwatch failed: %s", error.__class__.__name__)
            return False


    def active(self) -> list[dict[str, Any]]:
        """This account's active subscriptions, all pages. Never raises."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            for _ in range(20):  # at most 20 pages; this is a cleanup, not an export
                params: dict[str, Any] = {"status": "active", "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                response = self._request("GET", SUBSCRIPTIONS_URL, params=params)
                response.raise_for_status()
                body = response.json()
                found.extend(item for item in body.get("items") or [] if isinstance(item, dict))
                cursor = body.get("next_cursor")
                if not cursor:
                    break
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            logger.warning("Channel3 subscription list failed: %s", error.__class__.__name__)
        return found


def prune_watches(watcher: Channel3Watcher, has_pending_hold_for: Callable[[str], bool]) -> list[str]:
    """Cancel Leash's subscriptions whose product no request waits on any more.

    Only subscriptions Leash would have made (exactly its two event types)
    are touched; anything else on the Channel3 account is left alone.
    Returns the IDs cancelled.
    """
    cancelled = []
    for subscription in watcher.active():
        if sorted(subscription.get("events") or []) != sorted(WATCHED_EVENTS):
            continue
        product_id = subscription.get("product_id")
        subscription_id = subscription.get("subscription_id")
        if not product_id or not subscription_id or has_pending_hold_for(product_id):
            continue
        if watcher.unwatch(subscription_id):
            cancelled.append(subscription_id)
    return cancelled


def watch_sink(watcher: Channel3Watcher) -> Callable[[str, dict[str, Any]], None]:
    """An event sink that watches the product of every held request."""

    def sink(kind: str, record: dict[str, Any]) -> None:
        if kind != "decision.held":
            return
        catalog = record.get("catalog") or {}
        product_id = record.get("product_id")
        if catalog.get("status") != "ok" or not product_id:
            return  # nothing priced, so nothing to watch
        watcher.watch(product_id, catalog.get("sold_by") or None, catalog.get("selection"))

    return sink


def webhook_secret_from_env() -> str | None:
    secret = os.getenv("CHANNEL3_WEBHOOK_SECRET", "").strip()
    return secret or None


def watcher_from_env() -> Channel3Watcher | None:
    api_key = os.getenv("CHANNEL3_API_KEY", "").strip()
    if not api_key or not webhook_secret_from_env():
        return None
    return Channel3Watcher(api_key)


def verify_signature(
    secret: str, headers: Mapping[str, str], body: bytes, now: float | None = None
) -> bool:
    """Check a Svix (Standard Webhooks) signature. Constant-time compare.

    Signed content is ``{svix-id}.{svix-timestamp}.{body}``, HMAC-SHA256 with
    the base64-decoded part of the secret after ``whsec_``. The header holds
    one or more space-separated ``v1,<base64>`` values.
    """
    message_id = headers.get("svix-id") or headers.get("webhook-id") or ""
    timestamp = headers.get("svix-timestamp") or headers.get("webhook-timestamp") or ""
    signatures = headers.get("svix-signature") or headers.get("webhook-signature") or ""
    if not (message_id and timestamp and signatures):
        return False
    try:
        sent_at = int(timestamp)
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (ValueError, TypeError):
        return False
    if abs((time.time() if now is None else now) - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    signed = f"{message_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected):
            return True
    return False
