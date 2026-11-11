"""Small PayPal SANDBOX payouts client with local idempotency."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


# From PayPal's Payouts v1 schema (batch_enum and transaction_enum).
NOT_PAID_STATUSES = {"DENIED", "FAILED", "CANCELED", "BLOCKED", "RETURNED", "REFUNDED", "REVERSED"}
FINAL_STATUSES = {"SUCCESS"} | NOT_PAID_STATUSES
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
        database: Any | None = None,
    ) -> None:
        self.db_path = db_path or os.getenv("PAYOUTS_DB_PATH", "payouts.db")
        # Local idempotency records: payouts.db, or the gate's hosted Postgres
        # (leash.db.PostgresDatabase), so a restart never forgets a payout.
        if database is None:
            from leash.db import SqliteDatabase  # local import: payouts stays usable alone

            database = SqliteDatabase(self.db_path)
        self._db = database
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
        with self._db.connect() as connection:
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

        status, batch_id, error_name = self._post_payout(
            idempotency_key, receiver_email, amount, note
        )
        return self._save_record(idempotency_key, status, batch_id, error_name)

    def resend_payout(
        self,
        idempotency_key: str,
        receiver_email: str,
        amount_usd: str | Decimal | float,
        note: str,
    ) -> dict[str, Any]:
        """Send the same payout again, with the same IDs, to learn what happened.

        For a payout whose first send timed out or got a 5xx, so Leash never
        learned its batch ID. PayPal's Payouts API has no search by
        sender_batch_id; its documented recovery is to send again. It keeps
        PayPal-Request-Id keys for 30 days and never pays a sender_batch_id
        used in the last 30 days twice: a repeat returns the original batch,
        or an error linking to it. If the first send never reached PayPal,
        this one pays. The caller decides whether that is still allowed.
        """
        local = self._get_record(idempotency_key)
        if local and local.get("paypal_batch_id"):
            return local
        amount = self._format_amount(amount_usd)
        if self._dry_run:
            status, batch_id, error_name = "success", f"DRY_RUN_{idempotency_key}", None
        else:
            status, batch_id, error_name = self._post_payout(
                idempotency_key, receiver_email, amount, note, resending=True
            )
        if local is None:
            return self._save_record(idempotency_key, status, batch_id, error_name)
        with self._db.connect() as connection:
            connection.execute(
                "UPDATE payouts SET status = ?, paypal_batch_id = ?, error_name = ? "
                "WHERE idempotency_key = ?",
                (status, batch_id, error_name, idempotency_key),
            )
        return self._get_record(idempotency_key) or {}

    def _post_payout(
        self, idempotency_key: str, receiver_email: str, amount: str, note: str,
        *, resending: bool = False,
    ) -> tuple[str, str | None, str | None]:
        """POST one payout. Returns (local status, batch ID, PayPal error name)."""
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
            batch_header = response.json().get("batch_header", {})
            return (
                self._local_status(batch_header.get("batch_status")),
                batch_header.get("payout_batch_id"),
                None,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return "needs_reconcile", None, None
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            if code >= 500:
                return "needs_reconcile", None, None
            if resending:
                # The sender_batch_id was used already: PayPal links to the
                # original batch, which is the answer we were looking for.
                original = self._linked_batch_id(error.response)
                if original:
                    return "submitted", original, None
                if code in {401, 403} or self._mentions_sender_batch_id(error.response):
                    # Our credentials, or a batch that exists but whose link we
                    # could not read: either way not proof that nothing was paid.
                    return "needs_reconcile", None, None
            # A refusal of a sender_batch_id PayPal has not seen means no payout exists.
            return "failed", None, self._error_name(error)
        except (RuntimeError, ValueError):
            return "needs_reconcile", None, None

    @staticmethod
    def _error_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @classmethod
    def _mentions_sender_batch_id(cls, response: httpx.Response) -> bool:
        details = cls._error_body(response).get("details") or []
        return any(
            isinstance(d, dict) and str(d.get("field", "")).upper() == "SENDER_BATCH_ID"
            for d in details
        )

    @classmethod
    def _linked_batch_id(cls, response: httpx.Response) -> str | None:
        # Seen in the sandbox (2026-11): the duplicate-batch error is
        # USER_BUSINESS_ERROR with the link under details[].link, not in a
        # top-level "links". Accept both.
        body = cls._error_body(response)
        links = list(body.get("links") or [])
        for detail in body.get("details") or []:
            if isinstance(detail, dict):
                links += list(detail.get("link") or []) + list(detail.get("links") or [])
        prefix = "/v1/payments/payouts/"
        for link in links:
            path = httpx.URL(str(link.get("href", ""))).path if isinstance(link, dict) else ""
            if path.startswith(prefix):
                batch_id = path[len(prefix):].strip("/")
                if batch_id and "/" not in batch_id:
                    return batch_id
        return None

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
        """The batch ID this client already knows for a sender_batch_id.

        Local only: PayPal's Payouts API has no search by sender_batch_id.
        To learn about a send that never answered, use resend_payout.
        """
        local = self._get_record(sender_batch_id)
        if local and local.get("paypal_batch_id"):
            return {"payout_batch_id": local["paypal_batch_id"]}
        if self._dry_run:
            return {"payout_batch_id": f"DRY_RUN_{sender_batch_id}"}
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

            # The item's status decides when there is one: a SUCCESS batch can
            # still hold an item that is ONHOLD or PENDING.
            if (
                (transaction_status or batch_status) in FINAL_STATUSES
                or batch_status in NOT_PAID_STATUSES
                or error_name in FAILURE_NAMES
            ):
                # Seen in the sandbox: the item is SUCCESS while the batch
                # still says PROCESSING. The item is what was paid.
                if (transaction_status or batch_status) == "SUCCESS":
                    self._update_by_batch_id(paypal_batch_id, "success", None)
                elif (
                    batch_status in NOT_PAID_STATUSES
                    or transaction_status in NOT_PAID_STATUSES
                    or error_name in FAILURE_NAMES
                ):
                    not_paid = next(
                        (s for s in (transaction_status, batch_status) if s in NOT_PAID_STATUSES),
                        None,
                    )
                    self._update_by_batch_id(paypal_batch_id, "failed", error_name or not_paid)
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
        with self._db.connect() as connection:
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
        with self._db.connect() as connection:
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
        with self._db.connect() as connection:
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
            "CANCELED": "failed",
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
