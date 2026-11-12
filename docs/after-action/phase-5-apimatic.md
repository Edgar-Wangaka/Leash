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

## Follow-up: recovering a payout PayPal never answered (approved, done)

**Problem.** `find_payout_batch` called `GET /v1/payments/payouts?sender_batch_id=`,
which isn't in PayPal's schema. Only four endpoints exist: create a batch, get
a batch by ID, get an item, and cancel an item. So when the create call timed
out before Leash learned the batch ID, the payment stayed on "Checking with
PayPal" forever.

**Fix.** PayPal's documented recovery is to send the same request again.
PayPal never pays a `sender_batch_id` used in the last 30 days twice, keeps
`PayPal-Request-Id` keys for 30 days, and says a retry after a 5xx is safe.

- `find_payout_batch` is now local only.
- New `PayPalClient.resend_payout` POSTs the same body with the same
  `sender_batch_id` and `PayPal-Request-Id`, and reads PayPal's answer:
  - **201:** the batch ID.
  - **Duplicate-batch error:** the original batch, read from the error's link.
  - **5xx, timeout, 401 or 403:** still uncertain.
  - **Any other refusal:** failed, with PayPal's error name.
- The gate resends only payouts that are `needs_reconcile` with no batch ID.
  A `submitted` payout is never resent. Before resending, the gate checks:
  - **At most 5 attempts.** The count is saved before each send, so a crash
    still counts.
  - **Only within 7 days of the request**, well inside PayPal's 30-day memory.
  - **The hard rules still pass** (cap, seller allowlist, daily budget),
    because a resend pays if the first send never arrived. If the rules now
    fail, the payment stays on "Checking with PayPal" for a human. It isn't
    marked rejected, because the first send may have paid.
- The resend uses the same note as the first send, so the request body
  matches.

**Tests.** `tests/test_payout_resend.py` (13 cases):
- The resend uses the same IDs and the same body.
- The original batch ID is read from PayPal's duplicate error.
- A 5xx or 401 leaves the payment uncertain; a refusal marks it failed with
  no spend.
- A known batch is never POSTed again.
- The lookup makes no network call.
- Limits: the attempt limit, the 7-day window, and the rule recheck all stop
  a resend.
- A client without `resend_payout` keeps the old behaviour.
- A `submitted` payout is never resent.

Full suite after the follow-up: **271 passed, 1 skipped**.

## Sandbox run with a forced timeout (2026-11-11)

Run against `api-m.sandbox.paypal.com` with $1.00 sandbox payouts to an
allowlisted sandbox seller. The script is not in the repo; the steps are:

| Case | How it was forced | First run | After the fix |
|---|---|---|---|
| A: PayPal got it, the answer was lost | 1 ms read timeout on the POST | Resend marked it **failed**, but the batch existed and paid | Resend found batch `ELEGR8WDS9PRY`; item SUCCESS |
| B: never reached PayPal | Base URL pointed at a closed local port | Resend paid once (`CVRUBZXEXY9SA`); item SUCCESS | Same (`UVM28MMXM5LYN`); a second resend made no request |

**What the sandbox showed that the schema did not:**

1. **A resend with the same `PayPal-Request-Id` doesn't replay the first
   answer.** It returns 400 `USER_BUSINESS_ERROR`, with
   `details[0].field = SENDER_BATCH_ID`, the issue "Batch with given
   sender_batch_id already exists", and the link to the original batch under
   `details[0].link`. There's no top-level `links`. Leash only read top-level
   `links`, so it took this as a plain refusal and marked a paid payout
   **failed**, outside the daily spend. That was the worst outcome possible,
   and the mocked tests had used a guessed error shape.
   - **Fix:** read `details[].link` as well.
   - **Second guard:** on a resend, any error naming `SENDER_BATCH_ID` stays
     `needs_reconcile`, never `failed`, even if no link can be read.
2. **The item is SUCCESS while the batch is still PROCESSING.** The client's
   wait loop only recorded success once both said SUCCESS. It now goes by the
   item, as the gate already does.

Both are covered by tests that use the exact error body from the sandbox.
Full suite: **274 passed, 1 skipped**.

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
