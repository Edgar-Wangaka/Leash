# After-action: hackathon P3, real PayPal sandbox on Render

## Why

Render ran in dry run, so approvals made no PayPal call and showed fake IDs.
You switched Render to the PayPal sandbox (`DRY_RUN=false` and the sandbox
keys, set by you in Render).

## What was built

- **Live-money guard** (`config.check_live_money`, called at every start):
  - If `DRY_RUN` is off and `PAYPAL_BASE_URL` isn't one of PayPal's own
    sandbox hosts, Leash refuses to start. The message says what to change.
  - Only `LEASH_ALLOW_LIVE=true`, spelled exactly, overrides it.
  - `paypal_mode()` now goes by the host name. A URL that merely contains the
    word "sandbox", such as `sandbox.evil.example`, counts as live.
- **PayPal details kept:** `get_payout_status` now also returns the item's
  `transaction_id`, `payout_item_id` and `time_processed`. The reconciler
  already stores what it returns, and the dashboard shape (and OpenAPI)
  gained `paypal_transaction_id`, `paypal_status` (the item's status, not
  the batch's) and `paypal_time_processed`.
- **Dashboard:**
  - **Badge:** reads "PayPal sandbox: real PayPal, test money".
  - **Paid rows:** show the PayPal batch, the PayPal transaction, and "PayPal
    says SUCCESS".
  - **Seller line:** on rows and held cards, "Paid to the sandbox account
    standing in for selfridges.com". Real retailers have no sandbox account,
    so the stand-in is stated, not hidden.
- **`render.yaml`:** `DRY_RUN` is now set by you (sync: false), and
  `PAYPAL_BASE_URL` is pinned to the sandbox.

## Checks

- **Tests:** `pytest`, 352 passed and 8 skipped. New `tests/test_sandbox_mode.py`
  (15 cases):
  - the mode comes from the host name, and look-alike URLs count as live;
  - sandbox and dry run start;
  - live is refused without `LEASH_ALLOW_LIVE=true` exactly (5 near-misses);
  - live starts when it's allowed;
  - the transaction details are kept, using the sandbox's real response shape;
  - the dashboard shape shows the item's status.
- **Real sandbox run** (local gate, temporary database): an $8 request was
  allowed and sent to PayPal. The reconciler followed it to paid:
  - batch `6TZBTL4DR6S5U`
  - transaction `5JN15909WN528950J`
  - PayPal status SUCCESS, processed 21:33:04 UTC
- **Browser:** the page loads with the new lines and no console errors.

## Left for you

- **The hosted check:** on agentleash.me, send a test request and approve it
  if it's held. A real sandbox batch and transaction ID should appear, and the
  status should move to Paid within a few seconds. I can read the result from
  Neon afterwards to confirm.
- **Test buttons are held:** with a Channel3 key set, the three test buttons
  name no product, so they wait as "price unverified". Phase 4 replaces them
  with real-product runs.
