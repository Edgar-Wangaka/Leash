"""Shared runtime settings for the Leash entry points.

The MCP server is started by an agent host from an arbitrary working
directory, and the dashboard is started by a human from the project folder.
Both must open the same databases, so every path here is absolute.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


def _find_project_root() -> Path:
    """The folder holding config/. Works for a checkout and an installed package.

    From a source checkout (or an editable install) it is two levels above this
    file. A regular install puts this file in site-packages, so then the
    current directory is used; hosts such as Render start the app from the
    repository root. LEASH_PROJECT_ROOT overrides both.
    """
    configured = os.getenv("LEASH_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    source = Path(__file__).resolve().parents[2]
    if (source / "config" / "policy.json").exists():
        return source
    return Path.cwd().resolve()


PROJECT_ROOT = _find_project_root()
CONFIG_DIR = PROJECT_ROOT / "config"


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the project .env without overriding the shell."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _absolute(value: str) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


def configure() -> None:
    """Load .env and pin both database paths to absolute locations."""
    load_env()
    os.environ["LEASH_DB_PATH"] = _absolute(os.getenv("LEASH_DB_PATH", "leash.db"))
    os.environ["PAYOUTS_DB_PATH"] = _absolute(os.getenv("PAYOUTS_DB_PATH", "payouts.db"))
    check_live_money()


def leash_db_path() -> str:
    return os.environ["LEASH_DB_PATH"]


def dry_run() -> bool:
    return os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


SANDBOX_HOSTS = {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}


def paypal_mode() -> str:
    """Describe where money goes, for display. Never returns credentials.

    Only PayPal's own sandbox host names count as sandbox; any other host,
    PayPal's live API included, is treated as live money.
    """
    if dry_run():
        return "dry-run"
    base_url = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    host = (urlsplit(base_url.strip()).hostname or "").lower()
    return "sandbox" if host in SANDBOX_HOSTS else "live"


def check_live_money() -> None:
    """Refuse to start against live PayPal unless that was asked for in so many words.

    A hosted Leash can be switched from dry run to PayPal with one variable.
    One wrong base URL must not make it send real money.
    """
    if paypal_mode() == "live" and os.getenv("LEASH_ALLOW_LIVE", "").strip().lower() != "true":
        raise SystemExit(
            "PAYPAL_BASE_URL is not PayPal's sandbox and DRY_RUN is off. Leash would send "
            "real money. Use https://api-m.sandbox.paypal.com, set DRY_RUN=true, or set "
            "LEASH_ALLOW_LIVE=true if you really mean live payouts."
        )
