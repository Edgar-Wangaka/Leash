# Leash

Leash is a payment gate that sits between an AI agent and PayPal. It puts
spending limits and human approval in front of every payment the agent tries
to make.

An agent that shops reads product pages written by strangers, and a sentence
hidden in one of those pages can tell it to send money to someone else. With
Leash the agent never holds PayPal credentials. It can request a payment, and
a fixed set of rules decides what happens to the request:

| Outcome | When |
|---|---|
| Paid | The amount is below the approval threshold, the seller is approved, and the daily budget covers it |
| Held for a human | The amount is at or above the approval threshold. You approve or deny it in the dashboard |
| Blocked | The amount is over the per-payment cap or the daily budget, the seller is unknown, requests are arriving too fast, the task was never approved, or the amount is not a valid positive number |

The rules are ordinary code with no model in them, so nothing an agent reads
or writes can change the outcome.

## How it fits together

```
 Agent (Claude Desktop, or any MCP client)
        │  search_products ──► Channel3 product catalog (real products and prices)
        │  request_payment / get_payment_status
        ▼
 Leash MCP server ──► policy gate (config/policy.json) ──► PayPal Payouts API
                            │
                            ▼
                     SQLite decision log ◄── Dashboard (approve / deny)
```

- `src/leash/policy.py` holds the rules. They are pure functions that make no
  network calls and use no LLM.
- `src/leash/gate.py` reserves, pays and reconciles each request, and sends
  each payment once.
- `src/leash/mcp_server.py` defines the three tools an agent gets. None of
  them can approve a payment.
- `src/leash/dashboard.py` serves the local web page where you approve or deny.
- `src/leash/agent.py` searches products through the
  [Channel3](https://trychannel3.com) API.
- `src/payouts/paypal_client.py` is the PayPal Payouts client. It uses
  idempotency keys.

## Run it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[test]"
cp .env.example .env    # then fill in your PayPal sandbox credentials
```

Start the dashboard and open http://127.0.0.1:8787:

```bash
.venv/bin/python -m leash.dashboard
```

Set `DRY_RUN=true` in `.env` to try everything without calling PayPal. The
tag at the top right of the dashboard shows which mode is active.

### Connect an agent

Add this to the Claude Desktop config (`Settings → Developer → Edit Config`),
replacing `/ABSOLUTE/PATH/TO` with this folder, then restart Claude Desktop:

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

Then ask it to "Find a birthday gift under $10 and buy it." One listing in
the results carries a hidden instruction to send $500 to an attacker, and the
dashboard shows what Leash does with each request.

### Product search with Channel3

Set `CHANNEL3_API_KEY` in `.env` (free key at trychannel3.com) and
`search_products` returns real products and live prices from Channel3's
catalog, using the cheapest in-stock offer for each. Without a key, search
falls back to a three-item mock catalog, so the project still runs.

The dashboard also has a test bench: three buttons that send fixed requests
through the same gate, one of them a hijacked request. Use them when no model
is attached.

## Policy

The limits live in `config/policy.json`: per-payment cap, daily budget,
approved sellers, approval threshold, requests per minute, approval expiry and
approved task IDs. Whoever launches the MCP server sets the approved task
through `LEASH_TASK_ID`. The model and the product data cannot set it.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests cover each rule, double payment under concurrency, crash recovery
and approval expiry. They also check that a blocked request never reaches
PayPal.

## What is real and what is mocked

The policy gate, the decision log, the approval flow, the MCP server and the
PayPal Payouts sandbox integration are real. With `CHANNEL3_API_KEY` set, so
are the product names, descriptions, retailers and prices, which come from
Channel3.

Three things are mocked:

- Who gets paid. Real retailers have no PayPal sandbox account, so each
  retailer is mapped onto one of the approved sandbox sellers in
  `config/policy.json`. The money goes to that sandbox account and no real
  order is placed.
- The hostile listing. `config/planted_listings.json` is added to every
  Channel3 result on purpose, to show an injection attempt. Set
  `LEASH_PLANT_INJECTION=false` to turn it off.
- The fallback catalog in `config/mock_catalog.json`, and the test bench
  buttons, which send fixed requests and involve no AI.

An optional LLM risk check can escalate a payment to human review. It cannot
relax a rule, and it is off unless `LLM_BASE_URL` and `LLM_MODEL` are set.
