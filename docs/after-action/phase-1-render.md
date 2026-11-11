# After-action report: Render, phases 1–4

**Date:** 2026-11-11 · **Result:** code done, not yet deployed · **Tests:** 122 passed (23 new in `tests/test_remote.py`)

## What was planned
P1 deployable dashboard · P2 persistent state · P3 remote MCP · P4 demo hardening.

## What was done
| Phase | Item | Where |
|---|---|---|
| P1 | Bind host from `LEASH_DASHBOARD_HOST`, port from `PORT` | `dashboard.build_server` |
| P1 | Extra allowed hosts from `LEASH_PUBLIC_HOST` (comma-separated); `https://` Origin accepted only for those hosts | `dashboard.make_handler(..., public_hosts)` |
| P1 | Token from `LEASH_DASHBOARD_TOKEN` (min 16 chars), random fallback; the env token is never printed | `dashboard.dashboard_token` |
| P1 | Refuses to listen beyond loopback without `LEASH_PUBLIC_HOST` | `build_server` |
| P1 | Render Blueprint | `render.yaml` |
| P2 | Option A: Starter plan + 1 GB disk at `/var/data`, both DB paths on it | `render.yaml` |
| P3 | `LEASH_MCP_TRANSPORT=stdio\|streamable-http` (confirmed name in `mcp` 2.3.0) | `mcp_server.transport_from_env`, `main` |
| P3 | Bearer guard on the MCP HTTP app; agent token must differ from the dashboard token | `remote.require_bearer`, `remote.mcp_token_from_env` |
| P3 | One process, one port: `/mcp` → MCP, `/healthz`, everything else proxied to the dashboard on loopback | `src/leash/remote.py` (`python -m leash.remote`) |
| P4 | `GET /healthz` (no Host check, returns only `{"ok": true}`) | dashboard and front |
| — | README section with the Claude Desktop remote config | `README.md` → "Run it on another machine" |

## Design decisions
- **Proxy, not rewrite.** The dashboard stays on `http.server`, with its existing Host/Origin/token checks, bound to loopback. The ASGI front forwards Host and Origin untouched, so those checks still decide. All 7 existing dashboard tests pass unchanged.
- **MCP runs stateless with JSON responses** so it needs no sticky sessions and works behind Render's proxy.
- **DNS-rebinding protection stays on** for MCP, with the public host added to its allow list.
- **Two tokens, two doors.** The agent token is checked only by `/mcp`; the dashboard only takes `X-Leash-Token`. A test sends the agent token both ways to `/api/approve` and gets 401 each time.

## Verification
- Tests: public host + https Origin + token approves; no token → 401; `http://` Origin for the public host → 403; foreign and look-alike hosts → 403; health check answers any Host; env/random token; `0.0.0.0` without a public host refused; transport switch (stdio default, http runs uvicorn behind the guard, unknown name refused); token equality and absence refused; real MCP `initialize` + `tools/list` over HTTP lists exactly the three tools; the front serves the page, forwards approve, refuses the agent token and routes `/mcp`.
- Live local smoke run of `python -m leash.remote` on port 18080: `/healthz` 200, page 200, state 401 without token / 200 with it, attack simulate 200 (blocked), `/mcp` 401 without bearer / 200 `initialize` with it.

## Not done (needs you)
- Creating the Render service, entering secrets, and choosing Starter vs free.
- Custom domain from the Student Pack and its DNS.
- Keeping the instance warm during judging (only needed on the free plan; an uptime pinger hitting `/healthz` works).
- Connecting Claude Desktop to the deployed URL.
