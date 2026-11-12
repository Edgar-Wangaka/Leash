# After-action: hackathon P1, limits form and judge token

## Why

You couldn't see the Neon settings on the dashboard. The back end was finished
in S1 and S2 (`/api/settings`, Neon store, versions, history), but the dashboard
form ("S3") was never built, and both reports listed it as not done. Judges
also had no way in: the only human token is yours.

## What was built

**"Change limits" panel** (`src/leash/dashboard.html`), a fold-out under the
rules strip:
- **Fields:** cap, daily budget, approval threshold, requests per minute, hold
  expiry, and approved sellers (one per line). Each shows its allowed range
  from `settings.limits`.
- **Saving:**
  - **Tighten:** saves at once, and the bars update.
  - **Loosen:** a red box names the loosened limits and asks "Are you sure?".
    Only "Yes, loosen them" resends with `confirm_loosening: true`; Cancel
    changes nothing.
  - **Invalid value:** the server's reason appears next to the field, for
    example "can be at most 500.00".
  - **Store down:** "Nothing changed. Try again."
- **Back to defaults** sends `reset` and asks first if that would loosen
  anything.
- **History:** the last 10 versions, with who, when and what changed.
- **While you edit,** the 1.5-second refresh doesn't overwrite the form.

**Judge token** (`src/leash/dashboard.py`):
- `LEASH_DEMO_TOKEN` is a second human token, compared in constant time.
- Approvals, denials and limit changes made with it are recorded as
  `judge-demo`; yours stay `human-dashboard`.
- Startup refuses it if it's shorter than 16 characters or equal to the
  dashboard or MCP token. The agent's token is still refused everywhere.
- It's listed in `render.yaml` (sync: false) and the README.

**Postman:** 4 limit requests in Happy path (reset, tighten, loosen refused
without confirmation, loosen with confirmation back to the defaults), and 2
attacks (no token gets 401, beyond the allowed range gets 400 with the field
named).

**Local test server:** a `leash-dashboard-local-only` entry in
`.claude/launch.json` blanks every database URL. Your local `.env` has
`DATABASE_URL`, the real Neon database Render uses, so the normal dry-run
preview would have changed the live limits. I caught this before saving
anything.

## Checks

- **Tests:** `pytest`, 331 passed and 1 skipped. New `tests/test_demo_token.py`
  (14 cases):
  - a judge can read state, approve (recorded as `judge-demo`) and change
    limits (recorded as `judge-demo`);
  - the human is still recorded as the human;
  - a wrong, empty or agent token is refused;
  - with no demo token set, only the human gets in;
  - bad demo tokens stop startup.
- **Newman** against a local dashboard: 24 requests, 37 assertions, 0 failed.
- **Browser** (local SQLite), signed in with the local judge token:
  - budget 250 → 150 saved as v1;
  - 150 → 400 showed the confirm box, then saved as v2 after "Yes";
  - a $99,999 cap showed "can be at most 500.00";
  - history survived a reload;
  - the only console errors were the expected 409 and 400 responses.

## Not done yet

- **Your Render check:** set `LEASH_DEMO_TOKEN` in Render. Then change a limit
  on agentleash.me, redeploy, and confirm the version and history survive. That
  shows the Neon store is the one in use, because Render's own disk is wiped.
- **Running the Postman collection against Render** changes the live limits
  and then resets them to the defaults. Avoid it while judges are using the
  site.
