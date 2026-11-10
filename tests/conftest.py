"""Tests run against a fixed policy, not the live demo policy in config/."""

from pathlib import Path

import pytest

import leash.policy


@pytest.fixture(autouse=True)
def fixed_policy(monkeypatch):
    monkeypatch.setattr(
        leash.policy, "DEFAULT_POLICY_PATH", Path(__file__).with_name("policy.json")
    )
