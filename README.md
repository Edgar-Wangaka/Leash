# Leash

Leash is spending control for AI agents that pay vendors on a business's
behalf. It sits between the agent and PayPal: the agent can ask to pay, and a
fixed set of rules plus a human decide.

The first customer is a small business that lets an AI agent restock
supplies. The agent finds products and pays approved sellers; the owner sets
the limits and approves anything large. Paying a seller's PayPal account is
what PayPal Payouts is for, so approved payments go out through Payouts.
Checkout for shoppers (PayPal Orders) is the next integration.

An agent that shops reads product pages written by strangers, and a sentence
hidden in one of them can tell it to send money elsewhere. With Leash the
agent never holds PayPal credentials, and every request gets one of three
outcomes:

| Outcome | When |
|---|---|
| Paid | Below the approval threshold, an approved seller, and within the daily budget |
| Held for a human | At or above the approval threshold, a price Channel3 can't confirm, or a price more than 25% above the product's 30-day median |
| Blocked | Over the per-payment cap or daily budget, an unknown seller, too many requests, an unapproved task, an invalid amount, more than the live catalog price, or a different seller than the catalog lists |

The rules are plain code with no model in them, so nothing an agent reads or
writes can change the outcome.

## Try it

**Judges:** open https://www.agentleash.me and paste the judge access token
into the sign-in box. The token is in our hackathon submission, on the line
"Judge access token". It isn't in this repo, because the repo is public. It
signs you in as an approver (`judge-demo`), so you can run the agent, approve
or deny payments and change limits.

Everything runs on PayPal's sandbox with test money; the products are real
Channel3 listings.

1. **Small gift**: a Gemini agent searches Channel3 and buys a card under
   $15. It's paid at once, with PayPal's batch and transaction IDs.
2. **Gift hamper**: the agent finds a hamper around $50. It's over the $40
   threshold, so it waits under "Waiting for your approval".
3. **Over the cap**: the agent tries a $299.99 mixer and Leash blocks it
   (the cap is $100).
4. **Hijacked agent (no AI)**: a naive agent obeys hidden text on a product
   page and tries to pay $500 to `attacker@example.com`. Leash blocks it.
5. **Change limits**: lower the daily budget and watch the bar move.
   **Back to defaults** resets everything.

If the first load is slow, the free server was asleep.

## How it fits together

```
 Agent (Claude Desktop, or any MCP client)
        │  search_products ──► Channel3 catalog (real products and prices)
        │  request_payment / get_payment_status
        ▼
 Leash MCP server ──► policy gate (config/policy.json) ──► PayPal Payouts
                            │      ▲
                            │      └── Channel3 live price + 30-day history
                            ▼
                     decision log (SQLite or Neon) ◄── dashboard (approve / deny)
```

| File | Does |
|---|---|
| `src/leash/policy.py` | The rules: pure functions, no network, no LLM |
| `src/leash/gate.py` | Reserves, pays and reconciles each request; never pays twice |
| `src/leash/mcp_server.py` | The three agent tools. None can approve |
| `src/leash/dashboard.py`, `dashboard.html` | Approvals, limits, accounts, the AG Grid ledger |
| `src/leash/web_agent.py` | The "Ask the agent" box (Gemini, with a rule-based fallback) |
| `src/leash/catalog.py`, `price_watch.py` | Channel3 search, price check, price-change webhooks |
| `src/leash/remote.py` | One server for Render: dashboard, `/mcp`, webhooks |
| `src/payouts/paypal_client.py` | PayPal Payouts client with idempotency keys |

## Run it locally

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[test]"
cp .env.example .env    # PayPal sandbox keys; DRY_RUN=true skips PayPal
.venv/bin/python -m leash.dashboard
```

Open the `http://127.0.0.1:8787/#token=...` link it prints. The token is new
each start and the dashboard refuses approve, deny and state calls without it.

Connect Claude Desktop (`Settings → Developer → Edit Config`):

```json
{
  "mcpServers": {
    "leash": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/python",
      "args": ["-m", "leash.mcp_server"]
    }
  }
}
```

Or the hosted endpoint: `"type": "http"`, `"url": "https://www.agentleash.me/mcp"`
and an `Authorization: Bearer <LEASH_MCP_TOKEN>` header.

Optional keys in `.env`: `CHANNEL3_API_KEY` (real products; without it a
three-item mock catalog is used), `GEMINI_API_KEY` (the agent box; without it
a rule-based agent runs), `CHANNEL3_WEBHOOK_SECRET` (price-change alerts on
held payments), `KERNEL_API_KEY` (run the injection demo in a cloud browser
with `python -m leash.demo_injection --live`).

## Deploy (Render + Neon)

`render.yaml` is a Blueprint for one web service running
`python -m leash.remote`. Set these secrets in Render:

- `LEASH_DASHBOARD_TOKEN`: the owner's sign-in token (16+ characters).
- `LEASH_MCP_TOKEN`: the agent's token. The dashboard refuses it.
- `LEASH_DEMO_TOKEN` (optional): an approver token for judges, logged as
  `judge-demo`.
- `DATABASE_URL` and `DATABASE_URL_UNPOOLED` from [Neon](https://neon.com),
  plus `LEASH_SETTINGS_NAMESPACE=render`. The decision log, payout records,
  limits and accounts then survive restarts. Each copy of Leash uses its own
  schema (`leash_render`, `leash_local`), so a laptop never touches production.
- PayPal and Channel3 keys as in `.env`.

## Policy, limits and accounts

Defaults live in `config/policy.json`. The owner can change the cap, daily
budget, approval threshold, rate limit, hold expiry and sellers from **Change
limits** or `POST /api/settings`. Tightening applies at once; loosening needs
`"confirm_loosening": true`. Every change is versioned with who made it, and
the agent has no way to change a limit.

**Manage access** creates named tokens. Leash stores only their SHA-256 hash.

| Role | Can |
|---|---|
| Viewer | See the dashboard and ledger |
| Approver | Also approve or deny, run the agent, change limits |
| Owner | Also create and revoke accounts |

## API and attack tests (Postman)

[![Run in Postman](https://run.pstmn.io/button.svg)](https://app.getpostman.com/run-collection/59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9?action=collection%2Ffork&source=rip_markdown&collection-url=entityId%3D59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9%26entityType%3Dcollection%26workspaceId%3D8ebb3b4e-6964-4ee0-aba7-3a290a44eaa8)

`docs/openapi.yaml` describes the API (OpenAPI 3.1). The collection in
`postman/` has a happy path, an attack folder (wrong tokens, cross-site
requests, a hijacked $500 payment, approving a blocked request) and a "Live
attacks (no token)" folder. CI runs the first two with Newman on every push;
`.github/workflows/live-attacks.yml` runs the live folder against
agentleash.me every night.

## Tests

```bash
.venv/bin/python -m pytest
```

Over 400 tests cover each rule, the price checks, double payment under
concurrency, crash recovery, approval expiry, accounts, and that a blocked
request never reaches PayPal. `tests/test_db_postgres.py` runs the
concurrency checks against Postgres when `LEASH_TEST_DATABASE_URL` points at a
disposable Neon branch.

## What is real and what is mocked

Real: the rules, the decision log, approvals, the MCP server, the Gemini
agent, Channel3 products, prices and history, and PayPal sandbox payouts with
PayPal's own IDs.

Mocked, and labelled as such:

- **Who gets paid.** Real shops have no PayPal sandbox account, so each is
  mapped onto an approved sandbox seller in `config/policy.json`. No real
  order is placed.
- **The hostile listing.** `config/planted_listings.json` is added to search
  results on purpose. `LEASH_PLANT_INJECTION=false` turns it off.
- **The "Hijacked agent (no AI)" button** plays a naive agent in fixed code.

Leash's PayPal code was reviewed with APIMatic's PayPal Context Plugin, which
found three status-handling bugs (now covered by
`tests/test_paypal_statuses.py`).
