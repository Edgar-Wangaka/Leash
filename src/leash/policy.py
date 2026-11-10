"""Deterministic payment policy rules. This module never calls an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


DEFAULT_POLICY_PATH = Path(__file__).parents[2] / "config" / "policy.json"
RULE_NAMES = (
    "invalid_amount",
    "cap",
    "daily_budget",
    "allowlist",
    "approval_threshold",
    "velocity",
    "duplicate_request_id",
    "unrequested_task",
    "price_mismatch",
    "price_unverified",
    "payee_mismatch",
)


@dataclass(frozen=True)
class PaymentRequest:
    request_id: str
    agent_id: str
    merchant_email: str
    amount_usd: Decimal | str | float
    reason: str
    source_task_id: str
    product_id: str | None = None


@dataclass(frozen=True)
class Decision:
    verdict: str
    reasons: list[str]


def amount_to_cents(amount_usd: Decimal | str | float) -> int | None:
    """Return the amount in whole cents, or None if it is not payable.

    Zero, negative, non-numeric and non-finite amounts are not payable. A
    negative amount in particular must never be recorded, because it would
    lower the spending counted against the daily budget.
    """
    try:
        amount = Decimal(str(amount_usd))
        if not amount.is_finite():
            return None
        cents = int(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    except (InvalidOperation, ValueError):
        return None
    return cents if cents >= 1 else None


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load a policy JSON object without modifying it."""
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    with policy_path.open(encoding="utf-8") as policy_file:
        return json.load(policy_file)


def evaluate(request: PaymentRequest, state: Mapping[str, Any]) -> Decision:
    """Evaluate every deterministic rule and return all rules that fired."""
    policy = state["policy"]
    cents = amount_to_cents(request.amount_usd)
    if cents is None:
        # Nothing else can be judged without a real amount, and nothing is paid.
        return Decision(verdict="block", reasons=["invalid_amount"])
    amount = Decimal(cents) / Decimal(100)
    reasons: list[str] = []

    if amount > Decimal(str(policy["per_transaction_cap_usd"])):
        reasons.append("cap")

    daily_spend = Decimal(str(state.get("daily_spend_usd", "0")))
    if daily_spend + amount > Decimal(str(policy["daily_budget_usd"])):
        reasons.append("daily_budget")

    allowlist = {email.lower() for email in policy["merchant_allowlist"]}
    if request.merchant_email.lower() not in allowlist:
        reasons.append("allowlist")

    if amount >= Decimal(str(policy["approval_threshold_usd"])):
        reasons.append("approval_threshold")

    if int(state.get("recent_request_count", 0)) >= int(
        policy["max_requests_per_minute"]
    ):
        reasons.append("velocity")

    if request.request_id in set(state.get("seen_request_ids", ())):
        reasons.append("duplicate_request_id")

    if request.source_task_id not in set(state.get("approved_task_ids", ())):
        reasons.append("unrequested_task")

    # Catalog price check. The agent states a price; the catalog says what
    # the product really costs. Paying more than the catalog price is blocked.
    # A price that could not be checked is never paid without a human.
    catalog = state.get("catalog")
    if catalog and catalog.get("status") == "ok":
        tolerance = Decimal(str(policy.get("price_tolerance_pct", 5))) / Decimal(100)
        ceiling = Decimal(str(catalog["price_usd"])) * (Decimal(1) + tolerance)
        if amount > ceiling:
            reasons.append("price_mismatch")
        # The money must go to the seller the catalog names for this product,
        # not to some other approved seller the agent picked.
        seller = catalog.get("merchant_email")
        if seller and request.merchant_email.lower() != str(seller).lower():
            reasons.append("payee_mismatch")
    elif catalog:
        reasons.append("price_unverified")
    elif state.get("catalog_configured") and policy.get("require_catalog_product", True):
        # With a catalog configured, a request that names no product could be
        # for anything at any price, so it waits for a human by default.
        reasons.append("price_unverified")

    blocking_rules = {
        "invalid_amount",
        "price_mismatch",
        "payee_mismatch",
        "cap",
        "daily_budget",
        "allowlist",
        "velocity",
        "duplicate_request_id",
        "unrequested_task",
    }
    if any(reason in blocking_rules for reason in reasons):
        verdict = "block"
    elif "approval_threshold" in reasons or "price_unverified" in reasons:
        verdict = "hold_for_human"
    else:
        verdict = "allow"
    return Decision(verdict=verdict, reasons=reasons)
