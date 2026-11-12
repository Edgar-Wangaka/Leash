"""Every rule Leash can apply reads as plain words, never as its internal name."""

from __future__ import annotations

import pytest

from leash.explainer import explain
from leash.policy import RULE_NAMES

POLICY = {"per_transaction_cap_usd": 100, "daily_budget_usd": 250, "approval_threshold_usd": 40,
          "price_spike_pct": 25}


@pytest.mark.parametrize("rule", [*RULE_NAMES, "llm_suspicious"])
@pytest.mark.parametrize("verdict", ["block", "hold_for_human"])
def test_no_rule_name_leaks_into_an_explanation(rule, verdict):
    text = explain({"verdict": verdict, "reasons": [rule], "amount_usd": "50",
                    "policy_snapshot": POLICY, "catalog": None})
    assert rule not in text
    # "daily budget" and "approval threshold" are plain English in a sentence;
    # the other names would only appear as a leaked rule name.
    if rule not in {"daily_budget", "approval_threshold"}:
        assert rule.replace("_", " ") not in text
