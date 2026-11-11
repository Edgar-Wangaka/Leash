# After-action report: user-set limits, phase S1 (backend)

**Date:** 2026-11-11 · **Result:** done · **Tests:** 214 passed (37 new in `tests/test_settings.py`)

## What was planned
S1 backend: editable limits, checks, storage and API · S2 store them in a free hosted Postgres (Neon) so they survive Render restarts · S3 dashboard form · S4 (optional) delay before loosening takes effect, read-only MCP tool for the agent.

## What was done
| Item | Where |
|---|---|
| Six editable limits: per-payment cap, daily budget, approval threshold, requests per minute, hold expiry, approved sellers. Approved task IDs stay launcher-only | `settings.EDITABLE` |
| Checks: money ≥ 0 with at most 2 decimals; whole numbers in range; valid, de-duplicated seller emails; threshold ≤ cap ≤ budget; nothing above its `editable_limits` maximum. A maximum is never below the `policy.json` default it sits next to | `settings.validate`, `limits_for` |
| Loosening (any higher number, any new seller) needs `confirm_loosening: true`; tightening applies at once; `reset` returns to `policy.json` | `settings.loosened`, `LiveSettings.change` |
| Versioned store: every change is a row with the diff, who changed it and when | `SqliteSettingsStore` (table `policy_settings` in `leash.db`) |
| The gate's `policy` is now live: `policy.json` plus the latest version, cached for 2 s so the MCP process sees a change quickly. `policy.py` is unchanged | `PaymentGate.policy`, `LiveSettings` |
| A stored version that no longer fits `policy.json` is ignored with a warning, so editing the file can always pull limits back in | `LiveSettings._load` |
| If the store can't be read, the last limits read stay in force (not the possibly looser file); a change is refused with 503 | `LiveSettings.current` |
| `POST /api/settings`: 200 saved or unchanged, 400 invalid (an error per field), 409 loosening not confirmed, 503 store down; token and same-origin checks as for approve | `dashboard.py` |
| `/api/state` gains `settings`: current, defaults, ranges, labels, last 10 changes | `dashboard.settings_state` |
| Ranges written out under `editable_limits` in `config/policy.json` | `config/policy.json` |
| OpenAPI: `/api/settings`, `Limits`, `SettingsResult`, `Settings` schemas, `settings` tag | `docs/openapi.yaml` |
| A JSON body that isn't an object now gets 400 instead of an error on every POST route | `dashboard.py` |

## Checked by hand (throwaway dry-run dashboard, own test token)
```
1 tighten threshold to $20      200 saved, version 1
2 raise budget to $300           409 (not confirmed)
3 same, confirm_loosening:true   200
4 budget $5000                   400 "can be at most 2000.00"
5 no token                       401
7 /api/state                     threshold 20, budget 300, version 2, history [2, 1]
```
An $8 simulated payment was still held, for `price_unverified`. With a Channel3 key set, a request without a product ID waits for a human. That's existing behaviour, not a settings issue.

## Problems hit
- **Ceilings below the defaults.** The built-in maximum of 60 requests a minute was below a test policy's 100, so every saved version failed validation on reload and was silently ignored. A maximum now never sits below the `policy.json` value, and an ignored version is logged.
- **Fail-closed on outage.** The first draft fell back to `policy.json` when the store couldn't be read. That could undo a limit the human had tightened, so it now keeps the last limits read.

## Decisions
- **No events for limit changes yet.** The event bus and the Elastic sink only understand payment decisions (doc ID is `request_id`). Changes are audited in `policy_settings` and shown in `/api/state`. A `settings.changed` event can come with S2 or S3.
- **No MCP tool to change limits.** A test checks the MCP server never mentions `change_settings` or `/api/settings`.
- **Hosted copy:** on Render's free plan `leash.db` is wiped on each deploy and restart, so hosted changes reset until S2.

## Not done
- **S2:** a Neon Postgres store (needs you to create a free Neon project and put its connection string in `.env` and Render yourself).
- **S3:** the dashboard form, confirmation box, change history, and Postman requests and attack tests for `/api/settings`.
- **Deploy:** Render still runs the previous commit.
