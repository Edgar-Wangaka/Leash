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
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Protocol

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


class LiveSettings:
    """The limits the gate uses: policy.json plus the latest stored change.

    Reads are cached for ``ttl`` seconds, so another process (the MCP server
    next to the dashboard) sees a change within that time. A stored version
    that no longer fits policy.json's ranges is ignored, and the defaults
    apply, so editing policy.json can always pull the limits back in.
    """

    def __init__(self, file_policy: dict[str, Any], store: SettingsStore, ttl: float = 2.0) -> None:
        self.file_policy = file_policy
        self.store = store
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any], int | None] | None = None

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

    def current(self) -> tuple[dict[str, Any], int | None]:
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached[0] >= self.ttl:
                loaded = self._load()
                if loaded is not None:
                    self._cached = (now, *loaded)
                elif self._cached is not None:
                    # Keep the last limits read: the file defaults could be
                    # looser than a limit the human has tightened.
                    self._cached = (now, self._cached[1], self._cached[2])
                else:
                    self._cached = (now, dict(self.file_policy), None)
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
        if self._load() is None:
            return {"ok": False, "reason": "settings_store_unavailable"}
        with self._lock:
            self._cached = None
        current, _ = self.current()
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
        version = self.store.save(new, diff, changed_by)
        with self._lock:
            self._cached = None
        policy, _ = self.current()
        return {"ok": True, "reason": "saved", "version": version, "changes": diff,
                "loosened": looser, "policy": policy}
