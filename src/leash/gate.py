"""SQLite-backed policy gate for PayPal payouts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from payouts.paypal_client import PayPalClient

from .policy import Decision, PaymentRequest, evaluate, load_policy


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

    def process(self, request: PaymentRequest) -> dict[str, Any]:
        """Evaluate, record, and possibly pay a request."""
        existing = self._get_decision(request.request_id)
        if existing:
            return existing

        decision = evaluate(request, self._state())
        initial_status = "blocked" if decision.verdict == "block" else "pending"
        record = self._insert_decision(request, decision, initial_status)
        if decision.verdict == "allow":
            return self._pay(record, request)
        return record

    def approve(self, request_id: str) -> dict[str, Any]:
        """Approve a pending human-review request and send it exactly once."""
        record = self._get_decision(request_id)
        if not record:
            raise KeyError(f"Unknown request_id: {request_id}")
        if record["verdict"] != "hold_for_human":
            return record
        if record["status"] != "pending":
            return record

        request = PaymentRequest(**json.loads(record["request_json"]))
        return self._pay(record, request)

    def _pay(
        self, record: dict[str, Any], request: PaymentRequest
    ) -> dict[str, Any]:
        payout = self.payout_client.send_payout(
            request.request_id,
            request.merchant_email,
            request.amount_usd,
            request.reason,
        )
        status = str(payout.get("status", "failed"))
        self._update_decision(
            request.request_id,
            "paid" if status in {"success", "pending"} else "failed",
            payout.get("error_name"),
            payout,
        )
        return self._get_decision(request.request_id) or record

    def _state(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT request_id, amount_usd, created_at, status
                FROM decisions
                WHERE created_at >= datetime('now', '-1 day')
                """
            ).fetchall()
            recent = connection.execute(
                """
                SELECT COUNT(*) FROM decisions
                WHERE created_at >= datetime('now', '-1 minute')
                """
            ).fetchone()[0]

        return {
            "policy": self.policy,
            "daily_spend_usd": sum(
                (
                    Decimal(str(amount))
                    for _, amount, _, status in rows
                    if status in {"paid", "pending"}
                ),
                Decimal("0"),
            ),
            "recent_request_count": recent,
            "seen_request_ids": {request_id for request_id, *_ in rows},
            "approved_task_ids": set(self.policy.get("approved_task_ids", [])),
        }

    def _initialize_database(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    merchant_email TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_snapshot_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    payout_json TEXT,
                    error_name TEXT,
                    created_at TEXT NOT NULL
                )
                """
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
                INSERT INTO decisions (
                    request_id, agent_id, merchant_email, amount_usd,
                    verdict, reasons_json, status, policy_snapshot_json,
                    request_json, payout_json, error_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    request.request_id,
                    request.agent_id,
                    request.merchant_email,
                    str(request.amount_usd),
                    decision.verdict,
                    json.dumps(decision.reasons),
                    status,
                    json.dumps(self.policy, sort_keys=True),
                    request_json,
                    created_at,
                ),
            )
        return self._get_decision(request.request_id) or {}

    def _get_decision(self, request_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT request_id, agent_id, merchant_email, amount_usd,
                       verdict, reasons_json, status, policy_snapshot_json,
                       request_json, payout_json, error_name, created_at
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
            "verdict": row[4],
            "reasons": json.loads(row[5]),
            "status": row[6],
            "policy_snapshot": json.loads(row[7]),
            "request": json.loads(row[8]),
            "request_json": row[8],
            "payout": json.loads(row[9]) if row[9] else None,
            "error_name": row[10],
            "created_at": row[11],
        }

    def _update_decision(
        self,
        request_id: str,
        status: str,
        error_name: str | None,
        payout: dict[str, Any],
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE decisions
                SET status = ?, error_name = ?, payout_json = ?
                WHERE request_id = ?
                """,
                (status, error_name, json.dumps(payout), request_id),
            )
