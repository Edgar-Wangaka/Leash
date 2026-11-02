"""SQLite-backed policy gate for PayPal payouts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx

from payouts.paypal_client import PayPalClient

from .policy import (
    Decision,
    PaymentRequest,
    amount_to_cents,
    cents_to_amount,
    evaluate,
    load_policy,
)


HOLD_TTL_SECONDS = 15 * 60
STRICTNESS = {"allow": 0, "hold_for_human": 1, "block": 2}


class PaymentGate:
    """Evaluate requests before allowing the PayPal client to move money."""

    def __init__(
        self,
        *,
        db_path: str = "leash.db",
        policy_path: str | Path | None = None,
        payout_client: Any | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.policy_path = policy_path
        self.policy = load_policy(policy_path)
        self.payout_client = payout_client or PayPalClient()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._initialize_database()

    def close(self) -> None:
        close = getattr(self.payout_client, "close", None)
        if close:
            close()

    def __enter__(self) -> "PaymentGate":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def process(
        self,
        request: PaymentRequest,
        *,
        risk_signal: str | None = None,
        llm_check: Callable[[PaymentRequest], str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate, record, and possibly pay a request."""
        existing = self._get_decision(request.request_id)
        if existing:
            return existing

        decision = evaluate(request, self._state())
        llm_unavailable = False
        if decision.verdict != "block" and llm_check is not None:
            try:
                llm_check(request)
            except (TimeoutError, httpx.ReadTimeout):
                llm_unavailable = True
        decision = apply_risk_signal(decision, risk_signal)
        initial_status = "blocked" if decision.verdict == "block" else "pending"
        record = self._insert_decision(request, decision, initial_status)
        if llm_unavailable:
            self._update_decision(
                request.request_id,
                record["status"],
                "llm_unavailable",
                record.get("payout"),
            )
            record = self._get_decision(request.request_id) or record
        if decision.verdict == "allow":
            return self._pay(record, request)
        return record

    def approve(self, request_id: str, approved_by: str | None = None) -> dict[str, Any]:
        """Approve a pending human-review request and send it exactly once."""
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by is required")
        record = self._get_decision(request_id)
        if not record:
            raise KeyError(f"Unknown request_id: {request_id}")
        if record["verdict"] != "hold_for_human":
            return record
        if record["status"] != "pending":
            return record
        request = PaymentRequest(**json.loads(record["request_json"]))
        if approved_by == request.agent_id:
            self._update_decision(
                request_id,
                "rejected_on_recheck",
                "APPROVER_IS_AGENT",
                record.get("payout"),
            )
            return self._get_decision(request_id) or record
        if self._is_expired(record):
            self._update_decision(
                request_id,
                "rejected_on_recheck",
                "APPROVAL_EXPIRED",
                record.get("payout"),
                verdict=record["verdict"],
            )
            return self._get_decision(request_id) or record

        current_policy = load_policy(self.policy_path)
        current_state = self._state(
            exclude_request_id=request_id, policy=current_policy
        )
        recheck = evaluate(request, current_state)
        if recheck.verdict == "block":
            self._update_decision(
                request_id,
                "rejected_on_recheck",
                None,
                record.get("payout"),
                verdict=recheck.verdict,
                reasons=recheck.reasons,
                policy_snapshot=current_policy,
            )
            return self._get_decision(request_id) or record

        self.policy = current_policy
        return self._pay(record, request, approved_by=approved_by)

    def _pay(
        self,
        record: dict[str, Any],
        request: PaymentRequest,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        current = self._get_decision(record["request_id"])
        if not current:
            raise KeyError(f"Unknown request_id: {record['request_id']}")
        if current["verdict"] not in {"allow", "hold_for_human"}:
            return current
        if current["status"] != "pending":
            return current
        if current["verdict"] == "hold_for_human" and not approved_by:
            return current
        if current["verdict"] == "hold_for_human" and self._is_expired(current):
            self._update_decision(
                current["request_id"],
                "rejected_on_recheck",
                "APPROVAL_EXPIRED",
                current.get("payout"),
                verdict=current["verdict"],
            )
            return self._get_decision(current["request_id"]) or current

        request = PaymentRequest(**json.loads(current["request_json"]))
        if not self._claim_for_payment(request.request_id):
            return self._get_decision(request.request_id) or current
        self._update_decision(
            request.request_id, "paying", current.get("error_name"), None
        )
        payout = self.payout_client.send_payout(
            request.request_id,
            request.merchant_email,
            request.amount_usd,
            "Leash payout",
        )
        status = str(payout.get("status", "failed"))
        if status == "pending" and payout.get("paypal_batch_id"):
            self._update_decision(request.request_id, "submitted", None, payout)
            wait_for_final = getattr(self.payout_client, "wait_for_final_status", None)
            if wait_for_final:
                payout = wait_for_final(payout["paypal_batch_id"])
                status = str(payout.get("status") or self._status_from_final(payout))
        final_status = {
            "success": "paid",
            "failed": "failed",
            "needs_reconcile": "needs_reconcile",
            "pending": "submitted",
        }.get(status, "needs_reconcile")
        self._update_decision(
            request.request_id,
            final_status,
            payout.get("error_name") or current.get("error_name"),
            payout,
        )
        return self._get_decision(request.request_id) or record

    def _state(
        self,
        *,
        exclude_request_id: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as connection:
            query = """
                SELECT request_id, amount_cents, created_at, status, request_json
                FROM decisions
                WHERE created_at >= datetime('now', '-1 day')
            """
            params: tuple[Any, ...] = ()
            if exclude_request_id:
                query += " AND request_id != ?"
                params = (exclude_request_id,)
            rows = connection.execute(query, params).fetchall()
            recent = connection.execute(
                """
                SELECT COUNT(*) FROM decisions
                WHERE created_at >= datetime('now', '-1 minute')
                """
            ).fetchone()[0]

        return {
            "policy": policy or self.policy,
            "daily_spend_usd": sum(
                (
                    cents_to_amount(amount_cents)
                    for _, amount_cents, _, status, _ in rows
                    if status in {"paid", "paying", "submitted"}
                ),
                Decimal("0"),
            ),
            "recent_request_count": recent,
            "seen_request_ids": {
                seen_id
                for request_id, _, _, _, request_json in rows
                for seen_id in {request_id, json.loads(request_json)["request_id"]}
            },
            "approved_task_ids": set(
                (policy or self.policy).get("approved_task_ids", [])
            ),
        }

    def _initialize_database(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    request_id TEXT PRIMARY KEY UNIQUE,
                    agent_id TEXT NOT NULL,
                    merchant_email TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_snapshot_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    payout_json TEXT,
                    error_name TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(decisions)")
            }
            if "amount_cents" not in columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN amount_cents INTEGER"
                )
                rows = connection.execute(
                    "SELECT request_id, amount_usd FROM decisions "
                    "WHERE amount_cents IS NULL"
                ).fetchall()
                for request_id, amount_usd in rows:
                    connection.execute(
                        "UPDATE decisions SET amount_cents = ? WHERE request_id = ?",
                        (amount_to_cents(amount_usd), request_id),
                    )
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE decisions ADD COLUMN expires_at TEXT")
            connection.execute(
                "UPDATE decisions SET expires_at = datetime(created_at, ?) "
                "WHERE expires_at IS NULL AND verdict = 'hold_for_human'",
                (f"+{HOLD_TTL_SECONDS} seconds",),
            )

    def _insert_decision(
        self, request: PaymentRequest, decision: Decision, status: str
    ) -> dict[str, Any]:
        created_at = self._now().astimezone(timezone.utc).isoformat()
        request_json = json.dumps(
            {**asdict(request), "amount_usd": str(request.amount_usd)},
            sort_keys=True,
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO decisions (
                    request_id, agent_id, merchant_email, amount_usd, amount_cents,
                    verdict, reasons_json, status, policy_snapshot_json,
                    request_json, payout_json, error_name, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    request.request_id,
                    request.agent_id,
                    request.merchant_email,
                    f"{cents_to_amount(amount_to_cents(request.amount_usd)):.2f}",
                    amount_to_cents(request.amount_usd),
                    decision.verdict,
                    json.dumps(decision.reasons),
                    status,
                    json.dumps(self.policy, sort_keys=True),
                    request_json,
                    (
                            (self._now().astimezone(timezone.utc)
                            + timedelta(seconds=HOLD_TTL_SECONDS))
                            .replace(microsecond=0)
                            .isoformat()
                        if decision.verdict == "hold_for_human"
                        else None
                    ),
                    created_at,
                ),
            )
        return self._get_decision(request.request_id) or {}

    def _get_decision(self, request_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT request_id, agent_id, merchant_email, amount_usd, amount_cents,
                       verdict, reasons_json, status, policy_snapshot_json,
                       request_json, payout_json, error_name, expires_at, created_at
                FROM decisions WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "request_id": row[0],
            "agent_id": row[1],
            "merchant_email": row[2],
            "amount_usd": row[3],
            "amount_cents": row[4],
            "verdict": row[5],
            "reasons": json.loads(row[6]),
            "status": row[7],
            "policy_snapshot": json.loads(row[8]),
            "request": json.loads(row[9]),
            "request_json": row[9],
            "payout": json.loads(row[10]) if row[10] else None,
            "error_name": row[11],
            "expires_at": row[12],
            "created_at": row[13],
        }

    def _update_decision(
        self,
        request_id: str,
        status: str,
        error_name: str | None,
        payout: dict[str, Any] | None,
        *,
        verdict: str | None = None,
        reasons: list[str] | None = None,
        policy_snapshot: dict[str, Any] | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            updates = ["status = ?", "error_name = ?", "payout_json = ?"]
            values: list[Any] = [status, error_name, json.dumps(payout) if payout else None]
            if verdict is not None:
                updates.append("verdict = ?")
                values.append(verdict)
            if reasons is not None:
                updates.append("reasons_json = ?")
                values.append(json.dumps(reasons))
            if policy_snapshot is not None:
                updates.append("policy_snapshot_json = ?")
                values.append(json.dumps(policy_snapshot, sort_keys=True))
            values.append(request_id)
            connection.execute(
                f"UPDATE decisions SET {', '.join(updates)} WHERE request_id = ?",
                values,
            )

    def _is_expired(self, record: dict[str, Any]) -> bool:
        created_at = datetime.fromisoformat(record["created_at"])
        created_expired = (
            self._now().astimezone(timezone.utc) - created_at
        ).total_seconds() >= HOLD_TTL_SECONDS
        expires_at = record.get("expires_at")
        if expires_at:
            return created_expired or self._now().astimezone(timezone.utc) >= datetime.fromisoformat(
                expires_at
            )
        return created_expired

    def _claim_for_payment(self, request_id: str) -> bool:
        with sqlite3.connect(self.db_path, timeout=30) as connection:
            cursor = connection.execute(
                """
                UPDATE decisions
                SET status = 'paying'
                WHERE request_id = ? AND status = 'pending'
                """,
                (request_id,),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _status_from_final(payout: dict[str, Any]) -> str:
        if payout.get("status") == "needs_reconcile":
            return "needs_reconcile"
        if (
            payout.get("batch_status") == "SUCCESS"
            and payout.get("transaction_status") == "SUCCESS"
        ):
            return "success"
        if payout.get("batch_status") in {"DENIED", "FAILED"} or payout.get(
            "transaction_status"
        ) in {"DENIED", "FAILED"} or payout.get("error_name"):
            return "failed"
        return "needs_reconcile"


def apply_risk_signal(decision: Decision, signal: str | None) -> Decision:
    """Apply risk signals monotonically: a signal may only tighten a verdict."""
    if signal != "suspicious" or STRICTNESS[decision.verdict] >= STRICTNESS["hold_for_human"]:
        return decision
    return Decision(verdict="hold_for_human", reasons=[*decision.reasons, signal])
