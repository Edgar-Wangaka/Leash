"""Event sinks for outside services. Each one is off unless its env var is set.

A sink is a callable ``(kind, payload) -> None``. It is run on a daemon
thread by ``events.emit`` and may raise; the error is logged and dropped.
No sink can approve, deny or block anything: it gets a copy, after the fact.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .events import SINK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

Sink = Callable[[str, dict[str, Any]], None]

# Reasons that mean someone tried to steer the money, not just a big amount.
ATTACK_REASONS = {"allowlist", "payee_mismatch", "price_mismatch", "unrequested_task"}


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


# --- Elastic -------------------------------------------------------------------

def elastic_url_from_cloud_id(cloud_id: str) -> str:
    """Turn an Elastic Cloud ID into the Elasticsearch HTTPS URL."""
    _, _, encoded = cloud_id.partition(":")
    decoded = base64.b64decode(encoded or cloud_id).decode()
    host, es_id, *_ = decoded.split("$")
    host, _, port = host.partition(":")
    return f"https://{es_id}.{host}" + (f":{port}" if port and port != "443" else "")


class ElasticSink:
    """Index every event into an Elasticsearch index as an audit log.

    Optional, and not part of the hackathon entry: it is off unless
    ELASTIC_URL (or ELASTIC_CLOUD_ID) and ELASTIC_API_KEY are set, and it has
    not been run against a live cluster.

    The document id is ``<request_id>:<event>``, so a retried event replaces
    its own document instead of adding a duplicate. Talks to the REST API
    with httpx, so it needs no extra package.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        index: str = "leash-decisions",
        http_client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("the Elasticsearch URL must be https://")
        if not api_key:
            raise ValueError("ELASTIC_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.index = index
        self._client = http_client
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.__name__ = "elastic"

    def document(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        catalog = payload.get("catalog") or {}
        reasons = list(payload.get("reasons") or [])
        amount = float(payload.get("amount_usd") or 0)
        price = catalog.get("price_usd")
        return {
            "@timestamp": self._now().isoformat(),
            "event": kind,
            "request_id": payload["request_id"],
            "agent_id": payload.get("agent_id"),
            "merchant_email": payload.get("merchant_email"),
            "amount_usd": amount,
            "blocked_amount_usd": amount if kind == "decision.blocked" else 0.0,
            "verdict": payload.get("verdict"),
            "status": payload.get("status"),
            "reasons": reasons,
            "attack_reasons": sorted(ATTACK_REASONS.intersection(reasons)),
            "is_attack": bool(ATTACK_REASONS.intersection(reasons)),
            "item": payload.get("reason"),
            "product_id": payload.get("product_id"),
            "explanation": payload.get("explanation"),
            "risk_signal": payload.get("risk_signal"),
            "approved_by": payload.get("approved_by"),
            "paypal_batch_id": payload.get("paypal_batch_id"),
            "channel3_status": catalog.get("status"),
            "channel3_price_usd": float(price) if price not in (None, "") else None,
            "channel3_sold_by": catalog.get("sold_by"),
            "decision_created_at": payload.get("created_at"),
        }

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        doc_id = quote(f"{payload['request_id']}:{kind}", safe="")
        client = self._client or httpx.Client()
        try:
            response = client.put(
                f"{self.base_url}/{self.index}/_doc/{doc_id}",
                json=self.document(kind, payload),
                headers={"Authorization": f"ApiKey {self.api_key}"},
                timeout=SINK_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        finally:
            if self._client is None:
                client.close()


# --- registry ------------------------------------------------------------------

def from_env() -> list[tuple[str, Sink]]:
    """Build every sink whose configuration is present in the environment."""
    built: list[tuple[str, Sink]] = []

    elastic_url = _env("ELASTIC_URL")
    cloud_id = _env("ELASTIC_CLOUD_ID")
    elastic_key = _env("ELASTIC_API_KEY")
    if (elastic_url or cloud_id) and elastic_key:
        try:
            url = elastic_url or elastic_url_from_cloud_id(cloud_id or "")
            built.append(("elastic", ElasticSink(
                url, elastic_key, index=_env("ELASTIC_INDEX") or "leash-decisions"
            )))
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            logger.warning("Elastic sink not started: %s", error.__class__.__name__)

    from .price_watch import watch_sink, watcher_from_env  # avoids an import cycle

    watcher = watcher_from_env()
    if watcher is not None:
        built.append(("channel3_price_watch", watch_sink(watcher)))

    return built
