# After-action: judging-panel fixes (run unsupervised, 2026-11-12)

The simulated panel scored Leash 42/60. You asked me to swap the ledger for
AG Grid and fix everything else while you were away. All of it is committed
on `sponsor-integrations`, deployed to https://www.agentleash.me, and
checked there.

## What changed

| Fix | What was done | Commit |
|---|---|---|
| **AG Grid ledger** | Hand-built table replaced with AG Grid Community 36.2.0, pinned with a subresource-integrity hash. Search across sellers and reasons; Paid/Held/Blocked chips; **Blocked, by reason** with per-rule counts; **Export CSV** (includes request ID and PayPal batch, transaction and status); readable rule labels; live rows that flash; phone layout. Cells use `textContent` only; a plain list shows if the CDN fails. | `c9e7d3e` |
| **Demo hard to break** | Agent runs counted **per person** (5 a minute each, 20 overall, 300 a day, all settable by env var). One retry on a model 429/5xx. If Gemini stays down, the rule-based agent makes the run and says so, and a payment already decided is never sent again. `docs/demo-script.md` for a 2-minute backup video. Postgres upgrade of `agent_runs` checked on the Neon test branch and live in production. | `8ec5634` |
| **Why Payouts** | Leash is pitched as spending control for agents that **pay vendors** for a small business; Payouts is the right tool for that, and Orders/checkout is named as next. README intro, dashboard subtitle and `docs/pitch.md` (pitch, what's real vs sandbox, sponsor tools actually used, paste-ready submission text). | `eb94c4c` |
| **Channel3 in the purchase** | The lookup keeps Channel3's offer link, shown as **Buy on <shop>** (http/https only, `noopener noreferrer`). Every ~30 minutes the server cancels Leash's own Channel3 subscriptions that nobody waits on. | `6839c5b` |
| **Postman** | New "Live attacks (no token)" folder (12 requests), a nightly GitHub Actions Newman run against the live site, CI scoped to its local folders, `/mcp` documented in the OpenAPI spec, collection and Render environment updated in your Postman workspace, **Run in Postman** button in the README. | `4185e5d`, `1289531` |
| **Render, Elastic** | `docs/render-github-autodeploy.md` (install the Render GitHub App so pushes deploy). Elastic dropped: removed from `render.yaml`, and the sink is marked optional. | `45c0fe2` |
| **Better live demo** | "Try to trick it" never reached Leash because Gemini refuses obvious scams by itself. Added **Over the cap**: Gemini tried $299.99 (blocked, over the $100 cap), then tried a $99.99 "partial payment", which Leash held with the real $299.99 price shown. When the model refuses, the page now says Leash doesn't rely on that. | `7de36ca` |

## Checks

- **Tests:** `pytest`, 406 passed and 8 skipped (Postgres tests skip without a
  test URL). New tests cover the page (pinned, hash-checked AG Grid, no
  `innerHTML`), per-person and overall caps, the model retry and fallback,
  buy links, and subscription cleanup.
- **Browser (local, dry run, real Gemini and Channel3):**
  - **Small gift:** paid ($4.99).
  - **$60 hamper:** held ($49.99), then approved as `judge-demo`, and the
    ledger row turned "Paid, you approved".
  - **Over the cap:** blocked at $299.99; the $99.99 "partial payment" held.
  - **Try to trick it:** the model refused, and the note showed.
  - **Hijack:** blocked.
  - **Ledger:** AG Grid filter, search, blocked-by-reason, CSV and live rows
    all work, at desktop and 375 px widths, with no console errors.
- **Live site:**
  - Newman "Live attacks (no token)": 12 of 12 refused or answered as
    expected.
  - The page serves the AG Grid ledger, the new subtitle and the new
    examples.
  - Production Neon has the new `agent_runs.who` column.

## Re-score (same rubric)

| Criterion | Before | Now | Why |
|---|---|---|---|
| Problem and impact | 8 | 8 | Same problem, now with a named customer |
| Technical execution | 8 | 8 | Per-person caps and clean fallback; still one approver per deployment |
| Originality | 7 | 7 | The agent trying a partial payment to slip under the cap, and being caught, is a memorable live moment |
| Demo and polish | 6 | 8 | AG Grid ledger, demo that survives a busy model, script ready; the video is still yours to record |
| Theme fit | 7 | 8 | Payouts is now the natural PayPal tool for the story |
| Feasibility and future | 6 | 7 | Named customer, CSV audit export, per-person limits; no user accounts yet |
| **Total** | **42** | **46/60** | Solid entry that can place with a strong pitch |

**Sponsor prizes now:**
- Channel3: high.
- AG Grid: medium-high (real use, though nothing exotic).
- Render: medium-high once auto-deploy is connected.
- Postman: medium, high once the workspace is public.
- APIMatic: low-to-medium.
- Elastic and Bryntum: not claimed.

## Blocked on purpose (needs you)

- **Making the Postman workspace public.** The safety check refused it while
  you were away, because it publishes publicly. In Postman: workspace
  **Leash**, then **Settings** (or the workspace name menu), then
  **Visibility → Public**, then save. The README's Run in Postman button works
  after that.
- **Connecting Render's GitHub app:** see `docs/render-github-autodeploy.md`.
- **Merging the PR into `main`.** This also starts the keep-awake ping and
  the nightly live-attack run.
- **Recording the backup video** from `docs/demo-script.md`.
- **Pasting the submission text** from `docs/pitch.md`, with the judge token.
