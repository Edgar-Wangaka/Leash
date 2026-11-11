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
| Blocked | The amount is over the per-payment cap or the daily budget, the seller is unknown, requests are arriving too fast, the task was never approved, the amount is not a valid positive number, the amount is higher than the product's live price in the catalog, or the payment is addressed to a different seller than the catalog lists for that product |

The rules are ordinary code with no model in them, so nothing an agent reads
or writes can change the outcome.

## How it fits together

```
 Agent (Claude Desktop, or any MCP client)
        │  search_products ──► Channel3 product catalog (real products and prices)
        │  request_payment / get_payment_status
        ▼
 Leash MCP server ──► policy gate (config/policy.json) ──► PayPal Payouts API
                            │      ▲
                            │      └── Channel3 product lookup (live price check)
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
- `src/leash/catalog.py` talks to the [Channel3](https://trychannel3.com) API:
  product search for the agent, and product lookup for the price check.
- `src/leash/agent.py` holds the product searcher and a scripted shopping agent.
- `src/payouts/paypal_client.py` is the PayPal Payouts client. It uses
  idempotency keys.

## Run it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[test]"
cp .env.example .env    # then fill in your PayPal sandbox credentials
```

Start the dashboard, then open the link it prints:

```bash
.venv/bin/python -m leash.dashboard
```

The link looks like `http://127.0.0.1:8787/#token=...`. The token is made
fresh each time the server starts and is shown only in that terminal. The
dashboard refuses approve, deny and state calls without it, so another program
on your computer, such as an agent with a shell, cannot approve a payment by
calling the dashboard. It does not stop a program running as your user from
opening `leash.db` itself. To rule that out, run Leash as a separate user or on
another machine.

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

Leash also uses Channel3 to check the agent's work. The agent states a price
when it asks for a payment, and a fooled agent could state the wrong one. When
the request names a Channel3 product, Leash looks that product up itself and
compares the amount with the live price:

- If the amount is more than 5% above the live price, the request is blocked.
- If the product cannot be found or Channel3 does not answer, the request
  waits for a human.
- If the amount is at or below the live price, the other rules apply as usual.
- If the request pays a different seller than the one the catalog lists for
  the product, it is blocked.
- If no product is named at all, the request waits for a human.

The dashboard shows the retailer and live price that Channel3 returned next
to each request. Two settings in `config/policy.json` control the check:
`price_tolerance_pct` changes the 5%, and `require_catalog_product: false`
lets a request that names no product through without a human. With no
Channel3 key set, there is no catalog and none of these checks run. While a
key is set, the test request buttons below name no product, so they are held.

The dashboard also has three test request buttons that send fixed requests
through the same rules, one of them a hijacked request. Use them when no model
is attached.

## Run it on another machine (Render)

The full fix for "a program running as your user can open `leash.db`" is to
run Leash somewhere the agent has no shell. `render.yaml` sets that up as one
Render web service: `python -m leash.remote` serves the dashboard at `/`, the
MCP endpoint at `/mcp` and a health check at `/healthz` on one port, so the
dashboard, the reconcile poller and the MCP server share one database on one
persistent disk.

1. In Render, choose **New → Blueprint** and point it at this repository.
2. Fill in the secrets it asks for:
   - `LEASH_PUBLIC_HOST`: the name people use, e.g. `leash.onrender.com` or
     your custom domain.
   - `LEASH_DASHBOARD_TOKEN`: the human's approval token (16+ characters).
     Open `https://<host>/#token=<it>`.
   - `LEASH_MCP_TOKEN`: the agent's token. It must differ from the dashboard
     token. The dashboard refuses it, so an agent holding it cannot approve.
   - PayPal and Channel3 keys as in `.env`.
3. The Blueprint uses the free plan: the database is wiped on each deploy and
   the instance sleeps when idle. For a log that survives deploys, switch to
   the Starter plan (about $7/month) and add the disk described in
   `render.yaml`.

Point Claude Desktop at the remote MCP endpoint:

```json
{
  "mcpServers": {
    "leash": {
      "type": "http",
      "url": "https://leash.onrender.com/mcp",
      "headers": { "Authorization": "Bearer <LEASH_MCP_TOKEN>" }
    }
  }
}
```

To run only the MCP server over HTTP on your own machine, set
`LEASH_MCP_TRANSPORT=streamable-http` and `LEASH_MCP_TOKEN`, then start
`python -m leash.mcp_server` (it listens on `127.0.0.1:8788`).

## API spec and attack tests (Postman)

The dashboard API is described in `docs/openapi.yaml` (OpenAPI 3.1). The
Postman collection in `postman/` has two folders:

- **Happy path:** health, state, a small payment that pays, a large one that
  is held and then approved (and refused the second time), and one that is
  held and then denied.
- **Attacks:** no token, a wrong token, the agent's bearer-token style, a
  cross-site Origin, a form post, a foreign Host, a hijacked $500 request
  (blocked), and approving that blocked request (refused).

Import the collection and the `Local` or `Render` environment, put your
dashboard token in `dashboardToken` (a secret variable, empty in the repo),
and run the collection. One run sends 4 test requests and the policy allows
5 a minute, so wait a minute between runs. From the command line:

```bash
npx newman run postman/leash.postman_collection.json -e postman/render.postman_environment.json --env-var dashboardToken=<your token>
```

CI runs the same collection against a dry-run dashboard on every push.

## Watch an agent get hijacked in a real browser (KERNEL)

The planted listing is also a real product page, served by the dashboard at
`/demo/listing/premium-birthday-gift-box`. A person sees a gift box, a price
and a seller. The instruction to pay the attacker is on the page too, in
white-on-white text and in an HTML comment, where a scraping agent reads it.
Set `LEASH_DEMO_PAGES=false` to stop serving it.

```bash
.venv/bin/python -m leash.demo_injection --live
```

The live demo searches products, opens the listing page, plays an agent that
obeys what it finds hidden there, and sends the hijacked request to Leash,
which blocks it. It prints what was visible, what was hidden, the request
and the verdict.

To use a real cloud browser, install the extra (`pip install -e ".[kernel]"`;
no `playwright install` is needed, because KERNEL hosts the browser), put
`KERNEL_API_KEY` in `.env`, and point `LEASH_DEMO_BASE_URL` at a public Leash
deploy:

```bash
set -a; . ./.env; set +a
LEASH_DEMO_BASE_URL=https://leash-1pqh.onrender.com \
  .venv/bin/python -m leash.demo_injection --live --hold 30
```

The page then loads in a KERNEL browser. The demo prints the live-view link
as soon as the session starts; `--hold SECONDS` (up to 300) keeps the page
open that long so you can watch or record it next to the dashboard. The
session is deleted when the demo ends, and the link stops working. Wake a
free Render instance first with `curl https://leash-1pqh.onrender.com/healthz`.
Without a key, the page is rendered locally.

To open a real retailer page instead, run `--real`. It takes a Channel3
search result (needs `CHANNEL3_API_KEY`), opens its product page, and reports
where it landed, how much text was visible and hidden, whether the catalog
price is on the page, and whether the hidden text holds a payment instruction.
It never asks Leash to pay. Some retailers block automated browsers; `--real`
skips those (up to 3 tries) rather than trying to get past them. Channel3
links are affiliate redirects, so each open counts as a click.

```bash
.venv/bin/python -m leash.demo_injection --real
```

The scripted shopping agent and the LLM agent can take the same browser
(`ShoppingAgent(..., browse_fn=browse_page)`); the LLM agent then gets a
`browse_page` tool. Whatever a page says, the price and seller checks in the
gate still decide.

## Policy

The limits live in `config/policy.json`: per-payment cap, daily budget,
approved sellers, approval threshold, requests per minute, approval expiry and
approved task IDs. Whoever launches the MCP server sets the approved task
through `LEASH_TASK_ID`. The model and the product data cannot set it.

The daily budget covers the last 24 hours of committed money. A payment that
needed no approval counts from when it was requested; a held payment counts
from when you approved it. Payouts that PayPal reports as unclaimed still
count, because the money has left the account.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests cover each rule, the catalog price check, double payment under
concurrency, crash recovery and approval expiry. They also check that a blocked request never reaches
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
- The fallback catalog in `config/mock_catalog.json`, and the test
  request buttons, which send fixed requests and involve no AI.

An optional LLM risk check can escalate a payment to human review. It cannot
relax a rule, and it is off unless `LLM_BASE_URL` and `LLM_MODEL` are set.
