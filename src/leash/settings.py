"""Spending limits the human can change from the dashboard.

``config/policy.json`` holds the defaults and, under ``editable_limits``, the
highest values the dashboard may set. A change made in the dashboard is stored
as a new version in a settings store; the gate reads the latest version before
it decides. ``policy.evaluate`` is unchanged: it is simply handed these limits.

Only the dashboard (the human's token) can change them. The agent has no tool
for it, and the dashboard refuses the agent's token.

Rules for a change:
- every value is checked: money is 0 or more with at most 2 decimals, the
  approval threshold is at most the per-payment cap, the cap is at most the
  daily budget, nothing is above its maximum, sellers are email addresses;
- a change that loosens a limit (raises a number, adds a seller) is refused
  unless the request also confirms it. Tightening applies at once.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

MONEY_FIELDS = ("per_transaction_cap_usd", "daily_budget_usd", "approval_threshold_usd")
COUNT_FIELDS = ("max_requests_per_minute", "human_approval_expiry_hours")
EDITABLE = (*MONEY_FIELDS, *COUNT_FIELDS, "merchant_allowlist")

# Used when policy.json has no editable_limits block. Deliberately modest:
# the dashboard can never set more than these.
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    "per_transaction_cap_usd": {"max": 500},
    "daily_budget_usd": {"max": 2000},
    "approval_threshold_usd": {"max": 500},
    "max_requests_per_minute": {"min": 1, "max": 60},
    "human_approval_expiry_hours": {"min": 1, "max": 168},
    "merchant_allowlist": {"max": 25},
}
EMAIL = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")

LABELS = {
    "per_transaction_cap_usd": "Per-payment cap",
    "daily_budget_usd": "Daily budget",
    "approval_threshold_usd": "Approval threshold",
    "max_requests_per_minute": "Requests per minute",
    "human_approval_expiry_hours": "Hold expiry (hours)",
    "merchant_allowlist": "Approved sellers",
}


def limits_for(file_policy: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The allowed range of each editable field."""
    limits = {field: dict(bounds) for field, bounds in DEFAULT_LIMITS.items()}
    for field, bounds in (file_policy.get("editable_limits") or {}).items():
        if field in limits and isinstance(bounds, dict):
            limits[field].update(
                {k: v for k, v in bounds.items() if k in {"min", "max"} and _is_number(v)}
            )
    # policy.json's own value is always allowed, so a ceiling never sits
    # below the default it was written next to.
    for field in (*MONEY_FIELDS, *COUNT_FIELDS):
        value = file_policy.get(field)
        if _is_number(value):
            limits[field]["max"] = max(limits[field]["max"], value)
            if "min" in limits[field]:
                limits[field]["min"] = min(limits[field]["min"], value)
    sellers = file_policy.get("merchant_allowlist")
    if isinstance(sellers, list):
        limits["merchant_allowlist"]["max"] = max(limits["merchant_allowlist"]["max"], len(sellers))
    return limits


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


def _number_out(amount: Decimal) -> int | float:
    """Store whole dollars as ints, like policy.json does."""
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def validate(
    changes: dict[str, Any], current: dict[str, Any], file_policy: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply ``changes`` to ``current``. Returns (new values, errors by field)."""
    limits = limits_for(file_policy)
    errors: dict[str, str] = {}
    new = {field: current[field] for field in EDITABLE}

    unknown = sorted(set(changes) - set(EDITABLE))
    for field in unknown:
        errors[field] = "cannot be changed from the dashboard"

    for field in MONEY_FIELDS:
        if field not in changes:
            continue
        amount = _money(changes[field])
        top = Decimal(str(limits[field]["max"]))
        if amount is None:
            errors[field] = "must be an amount of 0 or more, with at most 2 decimals"
        elif amount > top:
            errors[field] = f"can be at most {top:.2f}"
        else:
            new[field] = _number_out(amount)

    for field in COUNT_FIELDS:
        if field not in changes:
            continue
        value = changes[field]
        low, high = int(limits[field]["min"]), int(limits[field]["max"])
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors[field] = "must be a whole number"
            continue
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            number = None
        if number is None or number != number.to_integral_value():
            errors[field] = "must be a whole number"
        elif not low <= number <= high:
            errors[field] = f"must be between {low} and {high}"
        else:
            new[field] = int(number)

    if "merchant_allowlist" in changes:
        sellers = changes["merchant_allowlist"]
        if isinstance(sellers, str):
            sellers = [part for part in re.split(r"[\s,;]+", sellers) if part]
        if not isinstance(sellers, list) or not all(isinstance(s, str) for s in sellers):
            errors["merchant_allowlist"] = "must be a list of email addresses"
        else:
            cleaned: list[str] = []
            for seller in sellers:
                seller = seller.strip().lower()
                if seller and seller not in cleaned:
                    cleaned.append(seller)
            bad = [seller for seller in cleaned if not EMAIL.match(seller) or len(seller) > 254]
            if bad:
                errors["merchant_allowlist"] = f"not an email address: {bad[0][:80]}"
            elif len(cleaned) > int(limits["merchant_allowlist"]["max"]):
                errors["merchant_allowlist"] = (
                    f"at most {int(limits['merchant_allowlist']['max'])} sellers"
                )
            else:
                new["merchant_allowlist"] = cleaned

    if not any(field in errors for field in MONEY_FIELDS):
        cap = Decimal(str(new["per_transaction_cap_usd"]))
        if Decimal(str(new["approval_threshold_usd"])) > cap:
            errors["approval_threshold_usd"] = "must not be above the per-payment cap"
        if cap > Decimal(str(new["daily_budget_usd"])):
            errors["per_transaction_cap_usd"] = "must not be above the daily budget"
    return new, errors


def loosened(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Fields where ``new`` lets more money move, or holds wait longer."""
    fields = [
        field for field in (*MONEY_FIELDS, *COUNT_FIELDS)
        if Decimal(str(new[field])) > Decimal(str(old[field]))
    ]
    old_sellers = {seller.lower() for seller in old["merchant_allowlist"]}
    if any(seller.lower() not in old_sellers for seller in new["merchant_allowlist"]):
        fields.append("merchant_allowlist")
    return fields


def changed(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        field: {"from": old[field], "to": new[field]}
        for field in EDITABLE
        if (
            sorted(s.lower() for s in old[field]) != sorted(s.lower() for s in new[field])
            if field == "merchant_allowlist"
            else Decimal(str(old[field])) != Decimal(str(new[field]))
        )
    }


# --- stores ----------------------------------------------------------------------

class SettingsStore(Protocol):
    def latest(self) -> dict[str, Any] | None: ...
    def save(self, values: dict[str, Any], changes: dict[str, Any], changed_by: str) -> int: ...
    def history(self, limit: int = 10) -> list[dict[str, Any]]: ...


class SqliteSettingsStore:
    """Versions of the limits in a table next to the decisions."""

    def __init__(self, db_path: str, now=None) -> None:
        self.db_path = db_path
        self._now = now or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS policy_settings (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    values_json TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    changed_by TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version, values_json FROM policy_settings ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"version": row[0], "values": json.loads(row[1])}

    def save(self, values: dict[str, Any], changes: dict[str, Any], changed_by: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_settings (values_json, changes_json, changed_by, changed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    json.dumps(values, sort_keys=True),
                    json.dumps(changes, sort_keys=True),
                    changed_by,
                    self._now().astimezone(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version, changes_json, changed_by, changed_at FROM policy_settings "
                "ORDER BY version DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {"version": r[0], "changes": json.loads(r[1]), "changed_by": r[2], "changed_at": r[3]}
            for r in rows
        ]


class PostgresSettingsStore:
    """Versions of the limits in a hosted Postgres database, such as Neon's free tier.

    Render's free plan wipes ``leash.db`` on every deploy and restart; this
    keeps the human's limits. One database can serve several Leash copies:
    each reads and writes only its own ``namespace``.
    """

    TABLE = "leash_policy_settings"

    def __init__(
        self, dsn: str, namespace: str, now=None, connect=None, migrate_dsn: str | None = None,
        names: tuple[str, str] = ("The settings database URL", "The direct settings database URL"),
    ) -> None:
        if not namespace or len(namespace) > 200:
            raise ValueError("a settings namespace of 1-200 characters is required")
        # dsn: Neon's pooled URL (PgBouncer) for everyday reads and writes.
        # migrate_dsn: the direct URL, for creating the table; Neon advises
        # schema changes over a direct connection, not the pooler.
        self.dsn = require_tls(dsn, names[0])
        self.migrate_dsn = require_tls(migrate_dsn, names[1]) if migrate_dsn else self.dsn
        self.namespace = namespace
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._connect_fn = connect
        self._ready = False
        self._conn = None
        self._lock = threading.Lock()

    # Reads go over the network (Nairobi to Oregon is several seconds for a
    # fresh TLS connection), so LiveSettings refreshes this store in the
    # background instead of making a payment decision wait for it.
    slow = True

    def _connection(self, dsn: str):
        if self._connect_fn is not None:
            return self._connect_fn(dsn)
        import psycopg  # the optional "postgres" extra

        # prepare_threshold=None: no server-side prepared statements, which
        # PgBouncer in transaction mode (Neon's pooler) does not support.
        return psycopg.connect(dsn, connect_timeout=10, autocommit=True, prepare_threshold=None)

    def _ensure_table(self) -> None:
        connection = self._connection(self.migrate_dsn)
        try:
            with connection.transaction():
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        version BIGSERIAL PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        values_json TEXT NOT NULL,
                        changes_json TEXT NOT NULL,
                        changed_by TEXT NOT NULL,
                        changed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {self.TABLE}_ns "
                    f"ON {self.TABLE} (namespace, version DESC)"
                )
        finally:
            connection.close()
        self._ready = True

    def _open(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connection(self.dsn)
        return self._conn

    def _drop(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _run(self, work, *, retry: bool):
        """Run ``work(connection)`` in a transaction on the kept connection.

        Neon closes idle connections (and suspends an idle database after
        5 minutes), so a read that fails is tried once more on a fresh
        connection. A write is never retried: it may have committed.
        """
        with self._lock:
            if not self._ready:
                self._ensure_table()
            for attempt in (1, 2):
                try:
                    connection = self._open()
                    with connection.transaction():
                        return work(connection)
                except Exception:
                    self._drop()
                    if not retry or attempt == 2:
                        raise

    def close(self) -> None:
        with self._lock:
            self._drop()

    def latest(self) -> dict[str, Any] | None:
        row = self._run(lambda c: c.execute(
            f"SELECT version, values_json FROM {self.TABLE} "
            "WHERE namespace = %s ORDER BY version DESC LIMIT 1",
            (self.namespace,),
        ).fetchone(), retry=True)
        if not row:
            return None
        return {"version": int(row[0]), "values": json.loads(row[1])}

    def save(self, values: dict[str, Any], changes: dict[str, Any], changed_by: str) -> int:
        row = self._run(lambda c: c.execute(
            f"INSERT INTO {self.TABLE} "
            "(namespace, values_json, changes_json, changed_by, changed_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING version",
            (
                self.namespace,
                json.dumps(values, sort_keys=True),
                json.dumps(changes, sort_keys=True),
                changed_by,
                self._now().astimezone(timezone.utc).isoformat(),
            ),
        ).fetchone(), retry=False)
        return int(row[0])

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._run(lambda c: c.execute(
            f"SELECT version, changes_json, changed_by, changed_at FROM {self.TABLE} "
            "WHERE namespace = %s ORDER BY version DESC LIMIT %s",
            (self.namespace, int(limit)),
        ).fetchall(), retry=True)
        return [
            {"version": int(r[0]), "changes": json.loads(r[1]), "changed_by": r[2],
             "changed_at": r[3]}
            for r in rows
        ]


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def clean_url(value: str) -> str:
    """Undo the usual paste mistakes: quotes from .env, Neon's ``psql '...'``."""
    value = value.strip()
    if value.startswith("psql "):
        value = value[5:].strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def require_tls(dsn: str, name: str = "The settings database URL") -> str:
    """Refuse a remote database URL that would send the password in clear text.

    Error messages name the variable, never its value.
    """
    parts = urlsplit(clean_url(dsn))
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise ValueError(f"{name} must be a postgres:// URL; check for extra text around it")
    if parts.hostname in LOCAL_HOSTS:
        return urlunsplit(parts)
    query = dict(parse_qsl(parts.query))
    mode = query.get("sslmode", "")
    if mode in {"disable", "allow", "prefer"}:
        raise ValueError(f"{name} needs sslmode=require or stricter")
    if not mode:
        query["sslmode"] = "require"
    return urlunsplit(parts._replace(query=urlencode(query)))


def default_namespace() -> str:
    """Which copy of Leash this is: its public host name, or "local"."""
    configured = os.getenv("LEASH_SETTINGS_NAMESPACE", "").strip()
    if configured:
        return configured
    public = os.getenv("LEASH_PUBLIC_HOST", "").split(",")[0].strip().lower()
    return public or "local"


URL_VARIABLES = (
    ("LEASH_SETTINGS_DATABASE_URL", "LEASH_SETTINGS_DATABASE_URL_UNPOOLED"),
    ("DATABASE_URL", "DATABASE_URL_UNPOOLED"),
)


def database_urls() -> tuple[str, str | None, tuple[str, str]]:
    """(pooled URL, direct URL, their variable names), or ("", None, ...).

    LEASH_SETTINGS_DATABASE_URL wins if set. Otherwise Neon's own variables,
    as `neon env pull` writes them: DATABASE_URL (pooled) and
    DATABASE_URL_UNPOOLED (direct).
    """
    for pooled, direct in URL_VARIABLES:
        value = clean_url(os.getenv(pooled, ""))
        if value:
            return value, clean_url(os.getenv(direct, "")) or None, (pooled, direct)
    return "", None, URL_VARIABLES[-1]


def store_from_env(db_path: str) -> SettingsStore:
    """Postgres when a settings database URL is set, else leash.db."""
    dsn, migrate_dsn, names = database_urls()
    if not dsn:
        return SqliteSettingsStore(db_path)
    try:
        import psycopg  # noqa: F401
    except ImportError as error:
        # Silently using leash.db would lose the human's limits on the next
        # restart without anyone noticing, so stop instead.
        raise SystemExit(
            'A settings database URL is set but psycopg is missing: pip install ".[postgres]"'
        ) from error
    try:
        return PostgresSettingsStore(
            dsn, default_namespace(), migrate_dsn=migrate_dsn, names=names
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


class LiveSettings:
    """The limits the gate uses: policy.json plus the latest stored change.

    Reads are cached for ``ttl`` seconds, so another process (the MCP server
    next to the dashboard) sees a change within that time. For a slow store
    (Postgres over the internet) only the first read waits: after that a
    stale copy is returned at once and refreshed on a background thread, so
    a payment decision never waits on the network. A change made through
    this object applies at once in this process either way.

    A stored version that no longer fits policy.json's ranges is ignored, and
    the defaults apply, so editing policy.json can always pull the limits
    back in.
    """

    def __init__(self, file_policy: dict[str, Any], store: SettingsStore, ttl: float = 2.0) -> None:
        self.file_policy = file_policy
        self.store = store
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any], int | None] | None = None
        self._refreshing = False
        self._background = bool(getattr(store, "slow", False))

    def defaults(self) -> dict[str, Any]:
        return {field: self.file_policy[field] for field in EDITABLE}

    def _load(self) -> tuple[dict[str, Any], int | None] | None:
        """The limits from the store, or None when the store can't be read."""
        try:
            stored = self.store.latest()
        except Exception as error:
            logger.warning("Settings store unreadable: %s", error.__class__.__name__)
            return None
        if not stored:
            return dict(self.file_policy), None
        values = {f: stored["values"].get(f, self.file_policy[f]) for f in EDITABLE}
        _, errors = validate(values, self.defaults(), self.file_policy)
        if errors:
            logger.warning(
                "Stored limits version %s no longer fit policy.json (%s); using the defaults",
                stored["version"], ", ".join(sorted(errors)),
            )
            return dict(self.file_policy), None
        return {**self.file_policy, **values}, stored["version"]

    def _store_result(self, loaded: tuple[dict[str, Any], int | None] | None) -> None:
        """Cache a load. Call with the lock held."""
        now = time.monotonic()
        if loaded is not None:
            self._cached = (now, *loaded)
        elif self._cached is not None:
            # Keep the last limits read: the file defaults could be looser
            # than a limit the human has tightened.
            self._cached = (now, self._cached[1], self._cached[2])
        else:
            self._cached = (now, dict(self.file_policy), None)

    def _refresh_in_background(self) -> None:
        try:
            loaded = self._load()
            with self._lock:
                self._store_result(loaded)
        finally:
            with self._lock:
                self._refreshing = False

    def current(self) -> tuple[dict[str, Any], int | None]:
        with self._lock:
            stale = self._cached is None or time.monotonic() - self._cached[0] >= self.ttl
            if stale and self._cached is not None and self._background:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh_in_background, daemon=True).start()
                return dict(self._cached[1]), self._cached[2]
            if stale:
                self._store_result(self._load())
            return dict(self._cached[1]), self._cached[2]

    def policy(self) -> dict[str, Any]:
        return self.current()[0]

    def change(
        self,
        changes: dict[str, Any],
        *,
        changed_by: str,
        confirm_loosening: bool = False,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Validate and store a change. Never raises for bad input."""
        if not changed_by or not changed_by.strip():
            return {"ok": False, "reason": "changed_by_required"}
        if not isinstance(changes, dict):
            return {"ok": False, "reason": "invalid", "errors": {"changes": "must be an object"}}
        # Validate against the latest stored version, read now, not a cached copy.
        loaded = self._load()
        if loaded is None:
            return {"ok": False, "reason": "settings_store_unavailable"}
        with self._lock:
            self._store_result(loaded)
        current = loaded[0]
        old = {field: current[field] for field in EDITABLE}
        new, errors = validate(self.defaults() if reset else changes, old, self.file_policy)
        if errors:
            return {"ok": False, "reason": "invalid", "errors": errors}
        diff = changed(old, new)
        if not diff:
            return {"ok": True, "reason": "unchanged", "changes": {}, "policy": current}
        looser = loosened(old, new)
        if looser and not confirm_loosening:
            return {"ok": False, "reason": "confirm_loosening", "loosened": looser, "changes": diff}
        try:
            version = self.store.save(new, diff, changed_by)
        except Exception as error:
            logger.warning("Settings store write failed: %s", error.__class__.__name__)
            return {"ok": False, "reason": "settings_store_unavailable"}
        policy = {**self.file_policy, **new}
        with self._lock:
            # This process uses the new limits at once, with no extra read.
            self._store_result((policy, version))
        return {"ok": True, "reason": "saved", "version": version, "changes": diff,
                "loosened": looser, "policy": policy}
