"""Manual smoke test for a one-dollar PayPal SANDBOX payout."""

from __future__ import annotations

import os
import sys
import uuid

from .paypal_client import PayPalClient


def main() -> int:
    from leash import config

    config.configure()  # load .env so the test works without exporting variables
    receiver_email = os.getenv("PAYPAL_RECEIVER_EMAIL")
    if not receiver_email:
        print("PAYPAL_RECEIVER_EMAIL must be set", file=sys.stderr)
        return 1

    key = f"smoke-{uuid.uuid4()}"
    with PayPalClient() as client:
        payout = client.send_payout(key, receiver_email, "1.00", "PayPal payout smoke test")
        print(f"Sent payout: {payout}")
        if not payout.get("paypal_batch_id"):
            print(f"Final status: {payout.get('status')}")
            return 0
        final_status = client.wait_for_final_status(payout["paypal_batch_id"])
        print(f"Final status: {final_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
