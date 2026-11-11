"""Tests run against a fixed policy, not the live demo policy in config/."""

from pathlib import Path

import pytest

import leash.events
import leash.policy


@pytest.fixture(autouse=True)
def fixed_policy(monkeypatch):
    monkeypatch.setattr(
        leash.policy, "DEFAULT_POLICY_PATH", Path(__file__).with_name("policy.json")
    )


@pytest.fixture(autouse=True)
def no_event_sinks():
    """No test sends events anywhere unless it registers a sink itself."""
    leash.events.clear()
    leash.events.reset_install_flag()
    yield
    leash.events.drain(1)
    leash.events.clear()
    leash.events.reset_install_flag()
