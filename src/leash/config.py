"""Shared runtime settings for the Leash entry points.

The MCP server is started by an agent host from an arbitrary working
directory, and the dashboard is started by a human from the project folder.
Both must open the same databases, so every path here is absolute.
"""

from __future__ import annotations

import os
from pathlib import Path


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


def leash_db_path() -> str:
    return os.environ["LEASH_DB_PATH"]


def dry_run() -> bool:
    return os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def paypal_mode() -> str:
    """Describe where money goes, for display. Never returns credentials."""
    if dry_run():
        return "dry-run"
    base_url = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    return "sandbox" if "sandbox" in base_url else "live"
