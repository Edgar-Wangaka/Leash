"""Small PayPal SANDBOX payouts client with local idempotency."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


FINAL_STATUSES = {"SUCCESS", "DENIED", "FAILED"}
FAILURE_NAMES = {"DENIED", "FAILED", "SELF_PAY_NOT_ALLOWED", "UNCLAIMED"}


class PayPalClient:
    """Client for PayPal payouts using OAuth client credentials."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.db_path = db_path or os.getenv("PAYOUTS_DB_PATH", "payouts.db")
        self._http = http_client or httpx.Client()
        self._owns_http_client = http_client is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._initialize_database()

    def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "PayPalClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _base_url(self) -> str:
        return os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")

    @property
    def _dry_run(self) -> bool:
        return os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}

    def _initialize_database(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS payouts (
                    idempotency_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    paypal_batch_id TEXT,
                    error_name TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _required_env(self, name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"{name} must be set")
        return value

    def get_access_token(self) -> str:
        """Return a cached OAuth token, refreshing it shortly before expiry."""
        if self._dry_run:
            return "dry-run-access-token"

        if self._access_token and self._monotonic() < self._token_expires_at:
            return self._access_token

        client_id = self._required_env("PAYPAL_CLIENT_ID")
        client_secret = self._required_env("PAYPAL_CLIENT_SECRET")
        response = self._http.post(
            f"{self._base_url}/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("PayPal token response did not contain access_token")

        expires_in = float(payload.get("expires_in", 300))
        self._access_token = token
        self._token_expires_at = self._monotonic() + max(expires_in - 60, 0)
        return token

    def send_payout(
        self,
        idempotency_key: str,
        receiver_email: str,
        amount_usd: str | Decimal | float,
        note: str,
    ) -> dict[str, Any]:
        """Send a payout once, returning the locally stored record on repeats."""
        existing = self._get_record(idempotency_key)
        if existing:
            return existing

        amount = self._format_amount(amount_usd)
        if self._dry_run:
            record = self._save_record(
                idempotency_key,
                "success",
                f"DRY_RUN_{idempotency_key}",
                None,
            )
            return record

        payload = {
            "sender_batch_header": {"sender_batch_id": idempotency_key},
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": receiver_email,
                    "amount": {"value": amount, "currency": "USD"},
                    "note": note,
                }
            ],
        }

        try:
            response = self._http.post(
                f"{self._base_url}/v1/payments/payouts",
                headers={
                    "Authorization": f"Bearer {self.get_access_token()}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": idempotency_key,
                },
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
            batch_header = response_payload.get("batch_header", {})
            batch_id = batch_header.get("payout_batch_id")
            status = self._local_status(batch_header.get("batch_status"))
            return self._save_record(idempotency_key, status, batch_id, None)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            return self._save_record(
                idempotency_key, "needs_reconcile", None, None
            )
        except httpx.HTTPStatusError as error:
            error_name = self._error_name(error)
            status = "needs_reconcile" if error.response.status_code >= 500 else "failed"
            return self._save_record(
                idempotency_key, status, None, error_name if status == "failed" else None
            )
        except (RuntimeError, ValueError) as error:
            return self._save_record(
                idempotency_key, "needs_reconcile", None, None
            )

    def get_payout_status(self, paypal_batch_id: str) -> dict[str, Any]:
        """Return batch status and the first payout item's transaction status."""
        if self._dry_run:
            return {
                "batch_status": "SUCCESS",
                "transaction_status": "SUCCESS",
                "error_name": None,
            }

        try:
            response = self._http.get(
                f"{self._base_url}/v1/payments/payouts/{paypal_batch_id}",
                headers={"Authorization": f"Bearer {self.get_access_token()}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            error_name = self._error_name(error)
            self._update_by_batch_id(paypal_batch_id, "needs_reconcile", error_name)
            return {
                "batch_status": None,
                "transaction_status": None,
                "error_name": error_name,
                "transport_error": True,
            }

        batch_header = payload.get("batch_header", {})
        item = (payload.get("items") or [{}])[0]
        error = item.get("errors") or {}
        return {
            "batch_status": batch_header.get("batch_status"),
            "transaction_status": item.get("transaction_status"),
            "error_name": error.get("name"),
        }

    def find_payout_batch(self, sender_batch_id: str) -> dict[str, Any] | None:
        """Find a batch by sender_batch_id for reconciliation."""
        local = self._get_record(sender_batch_id)
        if local and local.get("paypal_batch_id"):
            return {"payout_batch_id": local["paypal_batch_id"]}
        if self._dry_run:
            return {"payout_batch_id": f"DRY_RUN_{sender_batch_id}"}
        try:
            response = self._http.get(
                f"{self._base_url}/v1/payments/payouts",
                headers={"Authorization": f"Bearer {self.get_access_token()}"},
                params={"sender_batch_id": sender_batch_id},
            )
            response.raise_for_status()
            payload = response.json()
            batches = payload.get("items") or payload.get("batch_details") or []
            return batches[0] if batches else None
        except (httpx.HTTPError, ValueError, RuntimeError):
            return None

    def wait_for_final_status(
        self, paypal_batch_id: str, timeout_s: float = 120
    ) -> dict[str, Any]:
        """Poll until the payout reaches a final state or the timeout expires."""
        deadline = self._monotonic() + timeout_s
        while True:
            status = self.get_payout_status(paypal_batch_id)
            batch_status = status.get("batch_status")
            transaction_status = status.get("transaction_status")
            error_name = status.get("error_name")

            if (
                batch_status in FINAL_STATUSES
                or transaction_status in FINAL_STATUSES
                or error_name in FAILURE_NAMES
            ):
                if batch_status == "SUCCESS" and transaction_status == "SUCCESS":
                    self._update_by_batch_id(paypal_batch_id, "success", None)
                elif batch_status in {"DENIED", "FAILED"} or transaction_status in {
                    "DENIED",
                    "FAILED",
                } or error_name in FAILURE_NAMES:
                    self._update_by_batch_id(
                        paypal_batch_id,
                        "failed",
                        error_name or batch_status or transaction_status,
                    )
                return status

            if self._monotonic() >= deadline:
                self._update_by_batch_id(paypal_batch_id, "timeout", "TIMEOUT")
                return {**status, "timeout": True}

            self._sleep(min(5, max(0, deadline - self._monotonic())))

    def _format_amount(self, amount_usd: str | Decimal | float) -> str:
        try:
            amount = Decimal(str(amount_usd))
        except (InvalidOperation, ValueError) as error:
            raise ValueError("amount_usd must be a positive number") from error
        if not amount.is_finite() or amount <= 0:
            raise ValueError("amount_usd must be a positive number")
        return f"{amount:.2f}"

    def _get_record(self, idempotency_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT idempotency_key, status, paypal_batch_id, error_name, created_at
                FROM payouts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def _save_record(
        self,
        idempotency_key: str,
        status: str,
        paypal_batch_id: str | None,
        error_name: str | None,
    ) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO payouts
                    (idempotency_key, status, paypal_batch_id, error_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (idempotency_key, status, paypal_batch_id, error_name, created_at),
            )
        return self._get_record(idempotency_key) or {}

    def _update_by_batch_id(
        self, paypal_batch_id: str, status: str, error_name: str | None
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE payouts SET status = ?, error_name = ?
                WHERE paypal_batch_id = ?
                """,
                (status, error_name, paypal_batch_id),
            )

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "idempotency_key": row[0],
            "status": row[1],
            "paypal_batch_id": row[2],
            "error_name": row[3],
            "created_at": row[4],
        }

    @staticmethod
    def _local_status(status: str) -> str:
        return {
            "SUCCESS": "success",
            "DENIED": "failed",
            "FAILED": "failed",
            "UNCLAIMED": "unclaimed",
            "PENDING": "submitted",
            "PROCESSING": "submitted",
        }.get(status, "needs_reconcile")

    @staticmethod
    def _error_name(error: Exception) -> str | None:
        if isinstance(error, httpx.HTTPStatusError):
            try:
                payload = error.response.json()
                return payload.get("name")
            except ValueError:
                return None
        return None
