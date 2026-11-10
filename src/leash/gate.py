"""SQLite-backed policy gate for PayPal payouts."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from payouts.paypal_client import PayPalClient

from .explainer import explain, llm_risk_signal
from .policy import Decision, PaymentRequest, amount_to_cents, evaluate, load_policy

logger = logging.getLogger(__name__)

IN_PROGRESS_STATUSES = {"PENDING", "PROCESSING", "NEW"}
PAYPAL_STATUS_MAP = {
    "SUCCESS": "paid",
    "DENIED": "failed",
    "FAILED": "failed",
    "UNCLAIMED": "unclaimed",
}


class PaymentGate:
    """Evaluate requests before allowing the PayPal client to move money."""

    def __init__(
        self,
        *,
        db_path: str = "leash.db",
        policy_path: str | Path | None = None,
        payout_client: Any | None = None,
        now: Callable[[], datetime] | None = None,
        risk_signal: Callable[[PaymentRequest, str], tuple[str, str]] | None = None,
        price_lookup: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.policy = load_policy(policy_path)
        self.payout_client = payout_client or PayPalClient()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._risk_signal = risk_signal or llm_risk_signal
        # Looks a product up in the catalog. None means no catalog is configured.
        self._price_lookup = price_lookup
        self._initialize_database()

    def close(self) -> None:
        close = getattr(self.payout_client, "close", None)
        if close:
            close()

    def __enter__(self) -> "PaymentGate":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def process(self, request: PaymentRequest, *, task_text: str = "") -> dict[str, Any]:
        """Evaluate, reserve, and pay an allow decision."""
        catalog = self._check_catalog(request)
        decision, existing = self._reserve_decision(request, catalog)
        if existing is not None:
            return existing

        signal, signal_reason = "none", "not run"
        if decision.verdict != "block":
            try:
                signal, signal_reason = self._risk_signal(request, task_text)
            except Exception as error:
                logger.warning("LLM risk check failed: %s", error.__class__.__name__)
                signal_reason = f"LLM check failed: {error.__class__.__name__}"
            decision = self._merge_risk_signal(decision, signal, signal_reason)
            self._update_policy(request.request_id, decision, signal, signal_reason)

        record = self._get_decision(request.request_id) or {}
        if decision.verdict == "allow":
            result = self._pay(record, request)
            return result["record"]
        return record

    def _check_catalog(self, request: PaymentRequest) -> dict[str, Any] | None:
        """Ask the catalog what the named product costs. Never raises."""
        if not self._price_lookup or not request.product_id:
            return None
        try:
            return self._price_lookup(request.product_id)
        except Exception as error:
            logger.warning("Catalog lookup failed: %s", error.__class__.__name__)
            return {"status": "unavailable", "error": error.__class__.__name__}

    @staticmethod
    def _merge_risk_signal(
        decision: Decision, signal: str, signal_reason: str
    ) -> Decision:
        if signal != "suspicious":
            return decision
        verdict = "hold_for_human" if decision.verdict == "allow" else decision.verdict
        return replace(
            decision,
            verdict=verdict,
            reasons=[*decision.reasons, "llm_suspicious"],
        )

    def approve(self, request_id: str, approved_by: str) -> dict[str, Any]:
        """Atomically claim a human hold and pay it after a hard-rule recheck."""
        if not approved_by or not approved_by.strip():
            return self._result(False, "rejected", "approved_by_required", request_id)

        record = self._get_decision(request_id)
        if not record:
            return self._result(False, "not_found", "unknown_request_id", request_id)
        if approved_by == record["agent_id"]:
            return self._result(False, "rejected", "approver_is_requesting_agent", request_id)
        if record["verdict"] == "block":
            return self._result(False, "rejected", "blocked_request", request_id, record)
        if record["status"] == "paid":
            return self._result(False, "paid", "already_paid", request_id, record)
        if record["status"] == "denied":
            return self._result(False, "denied", "already_denied", request_id, record)
        if record["status"] == "expired":
            return self._result(False, "expired", "approval_expired", request_id, record)
        if record["verdict"] != "hold_for_human":
            return self._result(False, record["status"], "not_human_hold", request_id, record)

        now = self._now().astimezone(timezone.utc)
        if self._is_expired(record, now):
            self._set_status(request_id, "expired")
            return self._result(False, "expired", "approval_expired", request_id)

        with sqlite3.connect(self.db_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                """
                UPDATE decisions
                SET status = 'paying', approved_by = ?, approved_at = ?
                WHERE request_id = ? AND verdict = 'hold_for_human'
                  AND status = 'pending'
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (approved_by, now.isoformat(), request_id, now.isoformat()),
            ).rowcount
            connection.commit()

        if claimed != 1:
            latest = self._get_decision(request_id)
            reason = self._approval_status_reason(latest)
            return self._result(
                False,
                latest["status"] if latest else "not_found",
                reason,
                request_id,
                latest,
            )

        record = self._get_decision(request_id)
        hard_reasons = self._hard_recheck(record)
        if hard_reasons:
            self._set_rejected_on_recheck(request_id, hard_reasons)
            return self._result(
                False,
                "rejected_on_recheck",
                ",".join(hard_reasons),
                request_id,
            )
        return self._pay(record, None, already_claimed=True)

    def deny(self, request_id: str, denied_by: str, reason: str) -> dict[str, Any]:
        """Atomically deny a pending human hold."""
        if not denied_by or not denied_by.strip():
            return self._result(False, "rejected", "denied_by_required", request_id)
        if not reason or not reason.strip():
            return self._result(False, "rejected", "deny_reason_required", request_id)
        record = self._get_decision(request_id)
        if not record:
            return self._result(False, "not_found", "unknown_request_id", request_id)
        if denied_by == record["agent_id"]:
            return self._result(False, "rejected", "denier_is_requesting_agent", request_id)

        with sqlite3.connect(self.db_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                """
                UPDATE decisions
                SET status = 'denied', approved_by = ?, approved_at = ?,
                    error_name = ?
                WHERE request_id = ? AND verdict = 'hold_for_human'
                  AND status = 'pending'
                """,
                (
                    denied_by,
                    self._now().astimezone(timezone.utc).isoformat(),
                    reason,
                    request_id,
                ),
            ).rowcount
            connection.commit()
        latest = self._get_decision(request_id)
        if claimed != 1:
            return self._result(
                False,
                latest["status"] if latest else "not_found",
                "already_resolved",
                request_id,
                latest,
            )
        return self._result(True, "denied", None, request_id, latest)

    def reconcile(self, request_id: str) -> dict[str, Any]:
        """Query PayPal once and resolve submitted or uncertain payments."""
        record = self._get_decision(request_id)
        if not record:
            return self._result(False, "not_found", "unknown_request_id", request_id)
        if record["status"] not in {"submitted", "needs_reconcile", "paying"}:
            return self._result(False, record["status"], "not_reconcilable", request_id, record)

        batch_id = self._batch_id_for_request(request_id, record)
        if not batch_id:
            self._set_status(request_id, "needs_reconcile")
            return self._result(False, "needs_reconcile", "batch_id_unknown", request_id)
        status = self.payout_client.get_payout_status(batch_id)
        return self._update_from_reconcile(request_id, status)

    def poll_submitted(self) -> list[dict[str, Any]]:
        """Reconcile every submitted or uncertain row without resending."""
        with sqlite3.connect(self.db_path) as connection:
            request_ids = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT request_id FROM decisions
                    WHERE status IN ('submitted', 'needs_reconcile')
                    """
                ).fetchall()
            ]
        return [self.reconcile(request_id) for request_id in request_ids]

    def get(self, request_id: str) -> dict[str, Any] | None:
        """Return one stored decision, or None."""
        return self._get_decision(request_id)

    def list_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent decisions, newest first."""
        with sqlite3.connect(self.db_path) as connection:
            request_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT request_id FROM decisions "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            ]
        records = (self._get_decision(request_id) for request_id in request_ids)
        return [record for record in records if record]

    def daily_spend_usd(self) -> Decimal:
        """Money committed in the last 24 hours, as the budget rule counts it."""
        return self._state()["daily_spend_usd"]

    def _pay(
        self, record: dict[str, Any] | None, request: PaymentRequest | None, *, already_claimed: bool = False
    ) -> dict[str, Any]:
        """Claim a permitted row and submit it once."""
        request_id = (request.request_id if request else record["request_id"])
        current = self._get_decision(request_id)
        if not current:
            return self._result(False, "not_found", "unknown_request_id", request_id)

        if current["verdict"] == "block":
            return self._result(False, "blocked", "blocked_request", request_id, current)
        if current["status"] in {"paying", "submitted", "paid", "needs_reconcile"} and not (
            already_claimed and current["status"] == "paying"
        ):
            return self._result(False, current["status"], "already_in_progress_or_finished", request_id, current)
        if current["status"] != "pending" and not (
            already_claimed and current["status"] == "paying"
        ):
            return self._result(False, current["status"], "not_payable", request_id, current)
        if current["verdict"] == "hold_for_human":
            if not current["approved_by"]:
                return self._result(False, "rejected", "human_approval_required", request_id, current)
            if self._is_expired(current, self._now().astimezone(timezone.utc)):
                self._set_status(request_id, "expired")
                return self._result(False, "expired", "approval_expired", request_id)
        if current["verdict"] != "allow" and current["verdict"] != "hold_for_human":
            return self._result(False, "rejected", "verdict_not_payable", request_id, current)

        if not already_claimed:
            with sqlite3.connect(self.db_path, timeout=30) as connection:
                connection.execute("BEGIN IMMEDIATE")
                claimed = connection.execute(
                    "UPDATE decisions SET status = 'paying' "
                    "WHERE request_id = ? AND status = 'pending'",
                    (request_id,),
                ).rowcount
                connection.commit()
            if claimed != 1:
                latest = self._get_decision(request_id)
                return self._result(
                    False,
                    latest["status"] if latest else "not_found",
                    "already_in_progress_or_finished",
                    request_id,
                    latest,
                )

        receiver = current["merchant_email"]
        amount = Decimal(current["amount_cents"]) / Decimal(100)
        note = f"Leash payment {request_id[:8]}"
        try:
            payout = self.payout_client.send_payout(
                request_id, receiver, amount, note
            )
        except KeyboardInterrupt:
            raise
        except Exception as error:
            return self._finish_uncertain_or_failed(
                request_id, {"status": "needs_reconcile", "error_name": None}
            )

        status = str(payout.get("status", "needs_reconcile")).lower()
        if status == "success":
            status = "paid"
        if status == "failed":
            return self._finish_uncertain_or_failed(request_id, payout, definite=True)
        if status == "unclaimed":
            return self._finish_uncertain_or_failed(request_id, payout, final_status="unclaimed")
        if status == "paid":
            return self._finish_uncertain_or_failed(request_id, payout, final_status="paid")
        if status == "submitted":
            self._set_status(request_id, "submitted", payout)
            return self._result(True, "submitted", None, request_id)
        return self._finish_uncertain_or_failed(request_id, payout)

    def _finish_uncertain_or_failed(
        self,
        request_id: str,
        payout: dict[str, Any],
        *,
        definite: bool = False,
        final_status: str | None = None,
    ) -> dict[str, Any]:
        status = final_status or ("failed" if definite else "needs_reconcile")
        error_name = payout.get("error_name")
        self._set_status(request_id, status, payout, error_name)
        return self._result(status != "failed", status, error_name, request_id)

    def _apply_paypal_status(self, request_id: str, status: dict[str, Any]) -> dict[str, Any]:
        paypal_status = status.get("batch_status") or status.get("transaction_status")
        mapped = PAYPAL_STATUS_MAP.get(str(paypal_status).upper())
        error_name = status.get("error_name")
        if mapped is None:
            mapped = "needs_reconcile"
        self._set_status(request_id, mapped, status, error_name)
        return self._result(mapped == "paid", mapped, error_name, request_id)

    def _reserve_decision(
        self, request: PaymentRequest, catalog: dict[str, Any] | None = None
    ) -> tuple[Decision | None, dict[str, Any] | None]:
        with sqlite3.connect(self.db_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_id FROM decisions WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return None, self._get_decision(request.request_id)
            state = self._state(connection)
            state["catalog"] = catalog
            state["catalog_configured"] = self._price_lookup is not None
            decision = evaluate(request, state)
            status = "blocked" if decision.verdict == "block" else "pending"
            self._insert_decision(connection, request, decision, status, catalog)
            connection.commit()
            return decision, None

    def _state(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        exclude_request_id: str | None = None,
    ) -> dict[str, Any]:
        owns = connection is None
        connection = connection or sqlite3.connect(self.db_path)
        # created_at is stored as a UTC ISO-8601 string, so the window cutoffs
        # must be in the same format for the string comparison to be correct.
        now = self._now().astimezone(timezone.utc)
        day_ago = (now - timedelta(days=1)).isoformat()
        minute_ago = (now - timedelta(minutes=1)).isoformat()
        try:
            # Money counts against the budget from the moment it is committed:
            # an allowed request when it is reserved (created_at), a human hold
            # when an approval claims it (approved_at). Windowing a hold on its
            # creation time would let one approved late drop out of the budget
            # early. Unclaimed payouts have left the account, so they count too.
            rows = connection.execute(
                """
                SELECT request_id, amount_cents, status
                FROM decisions
                WHERE request_id IS NOT ?
                  AND (
                    (verdict = 'allow' AND created_at >= ? AND status IN (
                        'pending', 'paying', 'submitted', 'needs_reconcile',
                        'paid', 'unclaimed'))
                    OR (verdict = 'hold_for_human' AND approved_at >= ? AND status IN (
                        'paying', 'submitted', 'needs_reconcile', 'paid', 'unclaimed'))
                  )
                """,
                (exclude_request_id, day_ago, day_ago),
            ).fetchall()
            recent = connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE created_at >= ?",
                (minute_ago,),
            ).fetchone()[0]
            return {
                "policy": self.policy,
                "daily_spend_usd": sum(
                    (Decimal(cents) / Decimal(100) for _, cents, _ in rows), Decimal("0")
                ),
                "recent_request_count": recent,
                "seen_request_ids": {
                    row[0]
                    for row in connection.execute(
                        "SELECT request_id FROM decisions WHERE created_at >= ?",
                        (day_ago,),
                    ).fetchall()
                },
                "approved_task_ids": set(self.policy.get("approved_task_ids", [])),
            }
        finally:
            if owns:
                connection.close()

    def _hard_recheck(self, record: dict[str, Any] | None) -> list[str]:
        if not record:
            return ["unknown_request_id"]
        amount = Decimal(record["amount_cents"]) / Decimal(100)
        reasons: list[str] = []
        if amount > Decimal(str(self.policy["per_transaction_cap_usd"])):
            reasons.append("cap")
        if record["merchant_email"].lower() not in {
            email.lower() for email in self.policy["merchant_allowlist"]
        }:
            reasons.append("allowlist")
        state = self._state(exclude_request_id=record["request_id"])
        if state["daily_spend_usd"] + amount > Decimal(str(self.policy["daily_budget_usd"])):
            reasons.append("daily_budget")
        return reasons

    def _initialize_database(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    merchant_email TEXT NOT NULL,
                    amount_cents INTEGER,
                    amount_usd TEXT,
                    verdict TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_snapshot_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    payout_json TEXT,
                    error_name TEXT,
                    risk_signal TEXT NOT NULL DEFAULT 'none',
                    risk_reason TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    approved_by TEXT,
                    approved_at TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
            }
            additions = {
                "amount_cents": "INTEGER",
                "amount_usd": "TEXT",
                "approved_by": "TEXT",
                "approved_at": "TEXT",
                "expires_at": "TEXT",
                "risk_signal": "TEXT NOT NULL DEFAULT 'none'",
                "risk_reason": "TEXT NOT NULL DEFAULT ''",
                "explanation": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE decisions ADD COLUMN {name} {definition}")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_request_id_unique "
                "ON decisions(request_id)"
            )
            if "amount_cents" not in columns:
                connection.execute("UPDATE decisions SET amount_cents = CAST(ROUND(CAST(amount_usd AS REAL) * 100) AS INTEGER)")

    def _insert_decision(
        self,
        connection: sqlite3.Connection,
        request: PaymentRequest,
        decision: Decision,
        status: str,
        catalog: dict[str, Any] | None = None,
    ) -> None:
        # An unpayable amount is stored as 0 cents; its request is always blocked.
        amount_cents = amount_to_cents(request.amount_usd) or 0
        expires_at = None
        if decision.verdict == "hold_for_human":
            hours = float(self.policy.get("human_approval_expiry_hours", 24))
            expires_at = (self._now().astimezone(timezone.utc) + timedelta(hours=hours)).isoformat()
        request_json = json.dumps(
            {**asdict(request), "amount_usd": str(request.amount_usd), "catalog": catalog},
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO decisions (
                request_id, agent_id, merchant_email, amount_cents, amount_usd,
                verdict, reasons_json, status, policy_snapshot_json, request_json,
                payout_json, error_name, risk_signal, risk_reason, explanation,
                approved_by, approved_at, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'none', 'not run', ?, NULL, NULL, ?, ?)
            """,
            (
                request.request_id,
                request.agent_id,
                request.merchant_email,
                amount_cents,
                str(request.amount_usd),
                decision.verdict,
                json.dumps(decision.reasons),
                status,
                json.dumps(self.policy, sort_keys=True),
                request_json,
                explain({
                    "verdict": decision.verdict,
                    "reasons": decision.reasons,
                    "amount_usd": str(request.amount_usd),
                    "policy_snapshot": self.policy,
                    "catalog": catalog,
                }),
                expires_at,
                self._now().astimezone(timezone.utc).isoformat(),
            ),
        )

    def _get_decision(self, request_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT request_id, agent_id, merchant_email, amount_cents, amount_usd,
                       verdict, reasons_json, status, policy_snapshot_json, request_json,
                       payout_json, error_name, risk_signal, risk_reason, explanation,
                       approved_by, approved_at, expires_at, created_at
                FROM decisions WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "request_id": row[0], "agent_id": row[1], "merchant_email": row[2],
            "amount_cents": row[3], "amount_usd": row[4], "verdict": row[5],
            "reasons": json.loads(row[6]), "status": row[7],
            "policy_snapshot": json.loads(row[8]), "request": json.loads(row[9]),
            "request_json": row[9], "payout": json.loads(row[10]) if row[10] else None,
            "error_name": row[11], "risk_signal": row[12], "risk_reason": row[13],
            "explanation": row[14], "approved_by": row[15], "approved_at": row[16],
            "expires_at": row[17], "created_at": row[18],
        }

    def _update_policy(
        self, request_id: str, decision: Decision, signal: str, reason: str
    ) -> None:
        record = self._get_decision(request_id)
        explanation = explain({
            "verdict": decision.verdict,
            "reasons": decision.reasons,
            "amount_usd": record["amount_usd"],
            "policy_snapshot": self.policy,
            "catalog": record["request"].get("catalog"),
        })
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE decisions SET verdict = ?, reasons_json = ?, risk_signal = ?,
                    risk_reason = ?, explanation = ?, status = ?
                WHERE request_id = ?
                """,
                (
                    decision.verdict, json.dumps(decision.reasons), signal, reason,
                    explanation, "blocked" if decision.verdict == "block" else "pending",
                    request_id,
                ),
            )

    def _set_status(
        self, request_id: str, status: str, payout: dict[str, Any] | None = None,
        error_name: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE decisions SET status = ?, payout_json = COALESCE(?, payout_json),
                    error_name = COALESCE(?, error_name)
                WHERE request_id = ?
                """,
                (status, json.dumps(payout) if payout is not None else None, error_name, request_id),
            )

    def _set_rejected_on_recheck(self, request_id: str, reasons: list[str]) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE decisions SET status = 'rejected_on_recheck', reasons_json = ? WHERE request_id = ?",
                (json.dumps(reasons), request_id),
            )

    def _is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        return bool(record.get("expires_at") and record["expires_at"] <= now.isoformat())

    def _batch_id_for_request(self, request_id: str, record: dict[str, Any]) -> str | None:
        payout = record.get("payout") or {}
        if payout.get("paypal_batch_id"):
            return payout["paypal_batch_id"]
        finder = getattr(self.payout_client, "find_payout_batch", None)
        if finder:
            found = finder(request_id)
            if isinstance(found, dict):
                return found.get("payout_batch_id")
            return found
        return None

    def _approval_status_reason(self, record: dict[str, Any] | None) -> str:
        if not record:
            return "unknown_request_id"
        return {
            "paid": "already_paid",
            "paying": "already_in_progress",
            "submitted": "already_submitted",
            "denied": "already_denied",
            "expired": "approval_expired",
            "rejected_on_recheck": "failed_hard_rule_recheck",
        }.get(record["status"], "already_resolved")

    def _update_from_reconcile(self, request_id: str, status: dict[str, Any]) -> dict[str, Any]:
        # Keep what is already known about the payout (above all its batch ID)
        # so the next poll can still find it.
        previous = (self._get_decision(request_id) or {}).get("payout") or {}
        status = {**previous, **status}
        if status.get("transport_error"):
            mapped = "needs_reconcile"
            self._set_status(request_id, mapped, status)
            return self._result(False, mapped, None, request_id)
        transaction_status = str(status.get("transaction_status") or "").upper()
        batch_status = str(status.get("batch_status") or "").upper()
        paypal_status = (
            transaction_status
            if transaction_status in PAYPAL_STATUS_MAP
            else batch_status
        )
        mapped = PAYPAL_STATUS_MAP.get(str(paypal_status).upper(), "needs_reconcile")
        if mapped == "needs_reconcile" and (
            transaction_status in IN_PROGRESS_STATUSES
            or batch_status in IN_PROGRESS_STATUSES
        ):
            mapped = "submitted"  # PayPal has it and is still working on it
        self._set_status(request_id, mapped, status, status.get("error_name"))
        return self._result(
            mapped in {"paid", "submitted"}, mapped, status.get("error_name"), request_id
        )

    def _result(
        self, ok: bool, status: str, reason: str | None, request_id: str,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "status": status,
            "reason": reason,
            "record": record or self._get_decision(request_id),
        }
