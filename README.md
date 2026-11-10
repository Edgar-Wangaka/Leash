# Leash

**The agent can ask. Only the rules, and you, can pay.**

AI agents can now buy things. They can also be talked into buying the wrong
thing by text hidden in a product page. Leash sits between any agent and
PayPal. The agent never holds PayPal credentials. It can only *request* a
payment, and a deterministic rule engine decides what happens:

| Outcome | When |
|---|---|
| **Paid** | Small amount, approved seller, within budget |
| **Held for a human** | At or above the approval threshold; you approve or deny in the dashboard |
| **Blocked** | Over the per-payment cap, over the daily budget, unknown seller, too many requests, or a task nobody approved |

The rules contain no AI, so no prompt can argue with them.

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

- `src/leash/policy.py`: the rules. Pure functions, no network, no LLM.
- `src/leash/gate.py`: reserves, pays, and reconciles each request exactly once.
- `src/leash/mcp_server.py`: the three tools an agent gets. None can approve.
- `src/leash/dashboard.py`: local web page for the human.
- `src/leash/agent.py`: product search through the [Channel3](https://trychannel3.com) API.
- `src/payouts/paypal_client.py`: PayPal Payouts client with idempotency.

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
badge in the top right of the dashboard always shows which mode is active.

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

Then ask: *"Find a birthday gift under $10 and buy it."* One listing in the
results contains a hidden instruction to send $200 to an attacker. Watch the
dashboard.

### Product search with Channel3

Set `CHANNEL3_API_KEY` in `.env` (free key at trychannel3.com) and
`search_products` returns real products and live prices from Channel3's
catalog, using the cheapest in-stock offer for each. Without a key it falls
back to the three-item mock catalog, so everything still runs.

The dashboard also has three **Scripted agent** buttons that send fixed
requests through the same gate, including a hijacked one, for when no model
is attached.

## Policy

Edit `config/policy.json`: per-payment cap, daily budget, approved sellers,
approval threshold, requests per minute, approval expiry, and approved task
IDs. The approved task is set by whoever launches the MCP server
(`LEASH_TASK_ID`), never by the model or by product data.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests cover each rule, double-payment under concurrency, crash recovery,
approval expiry, and that a blocked request can never reach PayPal.

## What is real and what is mocked

- **Real:** the policy gate, the decision log, the approval flow, the MCP
  server, and the PayPal Payouts sandbox integration.
- **Real when `CHANNEL3_API_KEY` is set:** product names, descriptions,
  retailers and prices, from Channel3.
- **Mocked:**
  - *Who gets paid.* Real retailers have no PayPal sandbox account, so each
    retailer is mapped onto one of the approved sandbox sellers in
    `config/policy.json`. The money goes to that sandbox account, not to the
    retailer, and no real order is placed.
  - *The hostile listing.* `config/planted_listings.json` is added to every
    Channel3 result on purpose, to show an injection attempt. Set
    `LEASH_PLANT_INJECTION=false` to turn it off.
  - *The fallback catalog* (`config/mock_catalog.json`) and the
    scripted-agent buttons, which are not an AI.
- **Optional:** an LLM risk check that can escalate a payment to human review
  but can never relax a rule. It is off unless `LLM_BASE_URL` and `LLM_MODEL`
  are set.
