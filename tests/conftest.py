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


@pytest.fixture(autouse=True)
def no_hosted_settings_database(monkeypatch):
    """No test touches a real settings database, even if .env names one.

    An empty value wins over .env, because config.load_env never overrides a
    variable that is already set. The one Postgres test uses its own variable.
    """
    for name in ("LEASH_SETTINGS_DATABASE_URL", "LEASH_SETTINGS_DATABASE_URL_UNPOOLED",
                 "DATABASE_URL", "DATABASE_URL_UNPOOLED"):
        monkeypatch.setenv(name, "")
