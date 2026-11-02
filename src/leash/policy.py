"""Deterministic payment policy rules. This module never calls an LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


DEFAULT_POLICY_PATH = Path(__file__).parents[2] / "config" / "policy.json"
RULE_NAMES = (
    "cap",
    "daily_budget",
    "allowlist",
    "approval_threshold",
    "velocity",
    "duplicate_request_id",
    "unrequested_task",
)


@dataclass(frozen=True)
class PaymentRequest:
    request_id: str
    agent_id: str
    merchant_email: str
    amount_usd: Decimal | str | float
    reason: str
    source_task_id: str


@dataclass(frozen=True)
class Decision:
    verdict: str
    reasons: list[str]


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load a policy JSON object without modifying it."""
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    with policy_path.open(encoding="utf-8") as policy_file:
        return json.load(policy_file)


def evaluate(request: PaymentRequest, state: Mapping[str, Any]) -> Decision:
    """Evaluate every deterministic rule and return all rules that fired."""
    policy = state["policy"]
    amount = Decimal(str(request.amount_usd))
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

    blocking_rules = {
        "cap",
        "daily_budget",
        "allowlist",
        "velocity",
        "duplicate_request_id",
        "unrequested_task",
    }
    if any(reason in blocking_rules for reason in reasons):
        verdict = "block"
    elif "approval_threshold" in reasons:
        verdict = "hold_for_human"
    else:
        verdict = "allow"
    return Decision(verdict=verdict, reasons=reasons)
