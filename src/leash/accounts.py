"""Named access tokens: one account per person, each with a role.

The owner (whoever holds LEASH_DASHBOARD_TOKEN) creates an account for each
person who should use the dashboard, such as a judge, and Leash shows that
person's token exactly once. Leash stores only a SHA-256 hash of it, so a
copy of the database cannot be used to sign in. Every approval is recorded
under the account's name, and the owner can revoke an account at any time.

Roles:
- viewer: read the dashboard and the ledger;
- approver: also approve or deny held payments, run the agent and change limits;
- owner: also create and revoke accounts.

The agent's MCP token is separate and never maps to an account.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

ROLES = ("viewer", "approver", "owner")
PERMISSIONS = {
    "viewer": {"read"},
    "approver": {"read", "approve", "agent", "limits"},
    "owner": {"read", "approve", "agent", "limits", "accounts"},
}
TOKEN_PREFIX = "lsh_"
MAX_ACTIVE_ACCOUNTS = 50
MAX_NAME_CHARS = 60


def can(role: str | None, permission: str) -> bool:
    return permission in PERMISSIONS.get(role or "", set())


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    # Tokens are 256-bit random values, so a plain hash cannot be guessed back.
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(name: Any) -> str | None:
    text = " ".join(str(name or "").split())
    if not text or len(text) > MAX_NAME_CHARS or not text.isprintable():
        return None
    return text


def create_tables(connection: Any) -> None:
    """The accounts table, in SQL both SQLite and Postgres accept."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_hint TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_by TEXT,
            last_used_at TEXT
        )
        """
    )


def row_to_account(row: tuple) -> dict[str, Any]:
    return {
        "id": row[0], "name": row[1], "role": row[2], "token_hint": row[3],
        "created_by": row[4], "created_at": row[5], "revoked_at": row[6],
        "revoked_by": row[7], "last_used_at": row[8],
    }


ACCOUNT_COLUMNS = ("id, name, role, token_hint, created_by, created_at, "
                   "revoked_at, revoked_by, last_used_at")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return "acct_" + uuid.uuid4().hex[:12]
