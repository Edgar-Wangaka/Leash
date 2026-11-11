# After-action report: Zapier, phase 1 (phase 2 documented)

**Date:** 2026-11-11 · **Result:** sink done; Zaps need building in Zapier · **Tests:** 130 passed (8 new in `tests/test_zapier.py`)

## What was planned
P1 an outbound webhook sink for held and blocked events · P2 Zaps built in Zapier and documented · P3 (optional) Zapier MCP for harmless agent actions.

## What was done
| Item | Where |
|---|---|
| `ZapierSink`: POSTs flat JSON to `ZAPIER_HOOK_URL`, 3 s timeout | `src/leash/sinks.py` |
| Default kinds `decision.held` + `decision.blocked`; `ZAPIER_ALL_EVENTS=true` sends all | `sinks.from_env` |
| `is_attack` / `attack_reasons` from reasons `allowlist`, `payee_mismatch`, `price_mismatch`, `unrequested_task` | `sinks.ATTACK_REASONS` |
| `dashboard_url` from `LEASH_PUBLIC_HOST`, no token | `sinks.dashboard_url` |
| Refuses a non-https hook URL (logged, sink not started) | `ZapierSink.__init__` |
| README section with three Zap recipes | `README.md` → "Get told when a payment is held" |

## Tests added (httpx `MockTransport`, same pattern as `test_search.py`)
- held payment arrives with flat fields only (no nested dicts/lists) and a token-free dashboard link
- blocked attack is flagged with `allowlist`
- price inflation against a Channel3 price carries `channel3_price_usd` and `channel3_sold_by`
- paid payments are not sent by default
- neither the dashboard token nor the word "token" appears in any payload
- a 503 from Zapier leaves the decision unchanged
- `from_env` installs only when set, honours `ZAPIER_ALL_EVENTS`, refuses `http://`

## Not done
- **P2 Zaps:** they live in your Zapier account. Create a Catch Hook, set `ZAPIER_HOOK_URL`, press a simulate button in the dashboard so Zapier sees sample data, then build the three Zaps from the README. Screenshots for the README come after that.
- **P3 Zapier MCP:** skipped as optional. If added, it must stay limited to harmless actions; no inbound approve Zap.
