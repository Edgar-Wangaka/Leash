# After-action report: Postman, phases 1–2 (phase 3 partly)

**Date:** 2026-11-11 · **Result:** spec, collection and environments done and in your Postman account; nothing published publicly · **Tests:** 169 pytest passed (11 new in `tests/test_api_contract.py`); Newman 18 requests, 31 assertions, 0 failures

## What was planned
P1 an OpenAPI 3.1 spec · P2 a collection with Local and Render environments, Happy path and Attacks folders · P3 a public workspace with a "Run in Postman" button, plus a CI run.

## What was done
| Phase | Item | Where |
|---|---|---|
| P1 | OpenAPI 3.1 spec: `/healthz`, `/api/state`, `/api/approve`, `/api/deny`, `/api/simulate`, `/demo/listing/{slug}`; `X-Leash-Token` security scheme; shared error responses; `Decision` and `State` schemas | `docs/openapi.yaml` |
| P2 | Collection: Happy path (8 requests) and Attacks (10), each with `pm.test` assertions; request ids chained through collection variables | `postman/leash.postman_collection.json` |
| P2 | Local and Render environments; `dashboardToken` is a secret variable left empty | `postman/*.postman_environment.json` |
| P2 | Postman account: personal workspace **Leash** holding the collection, both environments and the spec in Spec Hub | workspace `8ebb3b4e-6964-4ee0-aba7-3a290a44eaa8` |
| P3 | CI job `postman`: starts a dry-run dashboard and runs the collection with Newman | `.github/workflows/ci.yml` |
| — | README section | `README.md` → "API spec and attack tests" |

## Attacks ported from `tests/test_dashboard.py`
No token → 401 · wrong token → 401 · `Authorization: Bearer` instead of the dashboard header → 401 · cross-site Origin → 403 · form post → 403 · foreign Host → 403 (or the host's edge refusing the name on Render) · simulated hijack → blocked for `allowlist` and `cap` · approving that blocked request → `blocked_request`.

## Verification
- **Newman run** against a local dry-run dashboard (no Channel3 key): 18/18 requests, 31/31 assertions passed.
- **Contract tests** keep three things in step:
  - the spec covers every route the handler serves
  - the `scenario` enum matches `SCENARIOS`, and the `Decision` schema matches `events.public_record` exactly
  - only `/healthz` and the demo page skip the token, and every `$ref` resolves
- **Collection tests** check that:
  - every request asserts something and targets a documented route
  - the attack folder covers the dashboard's attack tests
  - one run stays under the 5-a-minute rate limit
  - no token is committed

## Design decisions
- **The run is shaped by the live policy.** `config/policy.json` allows 5 requests a minute, so the collection sends 4 test requests. A test enforces that.
- **"Small is paid" is asserted only when no catalog is set.** With `CHANNEL3_API_KEY` set, test requests name no product and are held, as the README says.
- **Workspace kept personal.** A public workspace and the "Run in Postman" button publish your collection to everyone, so that waits for your go-ahead.
- PyYAML was added to the `test` extra for the spec tests.

## Not done
- **Run against Render with your token.** It needs `dashboardToken` set in the Render environment (in Postman, or via `--env-var`).
- **P3 public workspace and "Run in Postman" button:** needs your OK to publish.
- **P3 nightly Postman CLI run against Render:** needs a `POSTMAN_API_KEY` and the dashboard token as GitHub secrets. CI currently runs the collection against a local dry-run dashboard instead.
- APIMatic (shares this spec) is the next service in the plan.
