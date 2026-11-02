"""Run one sanitized live sandbox payout diagnostic."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
import argparse
from decimal import Decimal
from pathlib import Path

from leash.gate import PaymentGate
from leash.policy import PaymentRequest, load_policy
from payouts.paypal_client import PAYPAL_TIMEOUT, PayPalClient


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def _print_timeout(client: PayPalClient) -> None:
    timeout = client._http.timeout
    print(
        "effective_timeout "
        f"connect={timeout.connect}s read={timeout.read}s "
        f"write={timeout.write}s pool={timeout.pool}s"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one sanitized live sandbox payout diagnostic."
    )
    parser.add_argument("--receiver", help="allowlisted PayPal sandbox receiver email")
    return parser.parse_args(argv)


def _validate_receiver(receiver: str | None) -> str:
    if not receiver or not receiver.strip():
        raise SystemExit("receiver is required: use --receiver <email>")
    receiver = receiver.strip()
    policy = load_policy(Path(__file__).parents[1] / "config" / "policy.json")
    allowlist = {email.casefold() for email in policy["merchant_allowlist"]}
    if receiver.casefold() not in allowlist:
        raise SystemExit("receiver is not present in config/policy.json merchant_allowlist")
    sender = os.getenv("PAYPAL_SENDER_EMAIL") or os.getenv("SENDER_EMAIL")
    if sender and receiver.casefold() == sender.strip().casefold():
        raise SystemExit("receiver must not be the sender's own account")
    return receiver


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    receiver = _validate_receiver(args.receiver)
    print(f"receiver={receiver}")
    base_url = _required("PAYPAL_BASE_URL").rstrip("/")
    if base_url != "https://api-m.sandbox.paypal.com":
        raise SystemExit("PAYPAL_BASE_URL must be the PayPal sandbox URL")
    if os.getenv("DRY_RUN", "").strip().lower() != "false":
        raise SystemExit("DRY_RUN must be false")
    _required("PAYPAL_CLIENT_ID")
    _required("PAYPAL_CLIENT_SECRET")

    request_id = f"diag-{uuid.uuid4()}"
    decision_db = os.getenv("LEASH_DB_PATH", "leash-live.db")
    payout_db = os.getenv("PAYOUTS_DB_PATH", "payouts.db")
    diagnostic_path = Path(
        os.getenv("LEASH_PAYPAL_DIAG_PATH", "paypal-live-diag.jsonl")
    )
    diagnostic_path.unlink(missing_ok=True)
    os.environ["LEASH_PAYPAL_DIAG_PATH"] = str(diagnostic_path)

    print(f"request_id={request_id}")
    print(f"diagnostic_path={diagnostic_path}")
    print(f"configured_timeout=connect={PAYPAL_TIMEOUT.connect}s read={PAYPAL_TIMEOUT.read}s")

    request = PaymentRequest(
        request_id=request_id,
        agent_id="live-diagnostic",
        merchant_email=receiver,
        amount_usd=Decimal("1.00"),
        reason="Live sandbox diagnostic",
        source_task_id="demo-legitimate-task",
    )
    with PayPalClient(db_path=payout_db) as client:
        _print_timeout(client)
        with PaymentGate(db_path=decision_db, payout_client=client) as gate:
            result = gate.process(request)
        print(f"gate_status={result.get('status')}")

    with sqlite3.connect(decision_db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM decisions WHERE request_id = ?", (request_id,)
        ).fetchone()
    print("decisions_row=" + json.dumps(dict(row) if row else None, sort_keys=True))

    print("sanitized_capture:")
    if diagnostic_path.exists():
        for line in diagnostic_path.read_text(encoding="utf-8").splitlines():
            print(line)

    entries = [
        json.loads(line)
        for line in diagnostic_path.read_text(encoding="utf-8").splitlines()
    ] if diagnostic_path.exists() else []
    exception_classes = {entry.get("exception_class") for entry in entries}
    if "ReadTimeout" in exception_classes:
        hypothesis = "cause 2: read timeout"
    elif any(
        entry.get("status_code") == 404
        for entry in entries
    ):
        hypothesis = "cause 3: GET returned 404"
    elif any(
        entry.get("operation", "").startswith("GET")
        and entry.get("top_level_json_keys")
        and not entry.get("batch_status")
        for entry in entries
    ):
        hypothesis = "cause 3: GET returned an unready or unrecognizable body"
    elif any(
        entry.get("batch_status")
        and entry.get("batch_status")
        not in {"PENDING", "PROCESSING", "SUCCESS", "DENIED", "FAILED", "CANCELED"}
        for entry in entries
    ):
        hypothesis = "cause 4: unmapped status string"
    elif any(
        entry.get("operation", "").startswith("POST /v1/payments/payouts")
        and entry.get("status_code") == 201
        and not entry.get("payout_batch_id")
        for entry in entries
    ):
        hypothesis = "cause 1: POST 201 lacked expected response fields"
    else:
        hypothesis = "undetermined from sanitized capture"
    print(f"hypothesis={hypothesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
