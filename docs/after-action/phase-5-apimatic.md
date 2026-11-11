# After-action: APIMatic (PayPal Context Plugin)

## What the sponsor track is

The hackathon's APIMatic track is the **APIMatic Context Plugin for PayPal**
(`paypaldev/server-sdk-context-plugin-preview`). It gives a coding agent the
PayPal Server SDK's reference while it works. The original plan, generating
SDKs from `docs/openapi.yaml`, needs a paid APIMatic subscription: the CLI
login fails with "Error retrieving subscription info". That plan was dropped.

## What was done

1. **Installed the plugin in Claude Code.** Both official routes fail:
   - `npx context-plugins install <repo>`
   - `claude plugin install paypal@context-plugins`

   Both fail with `Unrecognized key: "displayName"`, because the preview's
   manifest has a field Claude Code's validator rejects. Workaround used:
   - Copy the plugin to `~/.claude/local-marketplaces/paypal-context`.
   - Delete `displayName` from `.claude-plugin/plugin.json`.
   - Add a one-entry `marketplace.json`.
   - Run `claude plugin marketplace add <dir>`, then `claude plugin install paypal@paypal-context-local`.

   The two broken marketplaces the failed attempts added were removed. The
   plugin holds only skills (no hooks, no MCP servers).
2. **Checked what the SDK covers.** PayPal Server SDK 2.4.0 has controllers
   for OAuth, Orders, Payments, Subscriptions, Transaction Search and Vault. It
   has **no Payouts**, so Leash's payout calls can't move to the SDK. Leash
   keeps its own small httpx client. That client was then checked against
   PayPal's published Payouts v1 schema.
3. **Fixed what the check found** in `src/leash/gate.py` and
   `src/payouts/paypal_client.py`.

## Findings and fixes

| # | Finding | Effect before | Fix |
|---|---|---|---|
| 1 | An item that was **ONHOLD** inside a batch marked **SUCCESS** was read by its batch status | Leash marked the payment **paid** while PayPal was still reviewing it | The item's own status decides; the batch status is used only when there is no item status |
| 2 | Statuses `CANCELED`, `BLOCKED`, `RETURNED`, `REFUNDED`, `REVERSED` were unknown | The payment stayed on "Checking with PayPal" forever and kept counting toward the daily budget | Mapped to `failed`, with the PayPal status as `error_name`; failed payments don't count as spend |
| 3 | The client's `wait_for_final_status` stopped when the batch said SUCCESS, even if the item was ONHOLD | The smoke test could report early | Waits on the item status; records the status that wasn't paid, not the batch's SUCCESS |

## Not fixed (needs a decision)

- **`find_payout_batch` calls `GET /v1/payments/payouts?sender_batch_id=`,
  which isn't in PayPal's schema.** Only four endpoints exist: create a batch,
  get a batch by ID, get an item, cancel an item. So when the create call
  times out before Leash learns the batch ID, the payment stays
  `needs_reconcile` (shown as "Checking with PayPal"). PayPal's documented
  recovery is to POST again with the same `sender_batch_id` and
  `PayPal-Request-Id`. PayPal keeps request keys for 30 days, never pays the
  same `sender_batch_id` twice, and on a 5xx says the retry is safe. That
  re-send moves money if the first call never reached PayPal, so it wasn't done
  without a review.
- The 4 s reconcile poller keeps asking about the stuck payment, and nothing
  times it out.

## Tests

- `tests/test_paypal_statuses.py` (new, 21 cases):
  - Every status in PayPal's `batch_enum` and `transaction_enum` is exactly one of: final, or still in progress.
  - Each not-paid status ends as `failed` with zero spend, and nothing is sent twice.
  - ONHOLD stays `submitted` and still counts toward the budget.
  - PayPal's `error_name` takes precedence over the status.
  - The client stops waiting on RETURNED and keeps waiting through ONHOLD.
- Full suite: **258 passed, 1 skipped**.

## Also changed

- `docs/openapi.yaml`: each tag has a description now, which clears 6 of 7
  Redocly warnings. The one left is that `/healthz` has no 4xx response, which
  is correct for that endpoint.
