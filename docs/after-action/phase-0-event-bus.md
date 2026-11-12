# After-action report: Phase 0, shared groundwork

**Date:** 2026-11-11 · **Result:** done · **Tests:** 99 passed (84 before, 15 new in `tests/test_events.py`)

## What was planned
An event bus that every sponsor integration hangs off, hook points in the gate, optional config, repo hygiene, CI and a devcontainer.

## What was done
| Item | Where |
|---|---|
| Event bus: `emit`, `register`, `drain`, `install_from_env` | `src/leash/events.py` |
| One public record shape for dashboard and sinks (moved `_public` out of the dashboard) | `events.public_record`, `dashboard.py` now aliases it |
| Sink registry, empty until each integration adds itself | `src/leash/sinks.py` |
| Gate hooks: first verdict in `process()`, status changes in `_set_status()`, approve claim, `deny()`, recheck rejection | `src/leash/gate.py` (`_notify`, `on_event` kwarg) |
| MCP server installs env-configured sinks once | `mcp_server._get_gate` |
| `*.db` in `.gitignore` (no db files were tracked) | `.gitignore` |
| Optional extras `remote`, `elastic`, `kernel` | `pyproject.toml` |
| GitHub Actions: pytest on 3.11 and 3.12, `DRY_RUN=true` | `.github/workflows/ci.yml` |
| Codespaces devcontainer, dry run, port 8787 forwarded | `.devcontainer/devcontainer.json` |

## Design decisions
- **Sinks run on daemon threads.** `emit()` returns at once. A sink that hangs, raises or mutates its payload has no effect on the decision or on other sinks (each gets its own copy). The 3 s timeout is enforced by each HTTP sink's own client timeout; `drain()` exists for tests and shutdown.
- **Events fire only on a real status change.** `_set_status` reads the previous status first, so the reconcile poller re-reading a still-`submitted` payout does not re-announce it.
- **Extra event kinds.** The plan listed six kinds. `approved`, `submitted`, `expired`, `unclaimed`, `needs_reconcile` and `rejected_on_recheck` were added so the audit log in Elastic has every transition.
- **`on_event` is injectable** on `PaymentGate`. Tests use a synchronous recorder; production uses `events.emit`.
- `policy.py` was not touched.

## Tests added
- each path emits exactly once: allow → created + paid; hold → held; block → blocked; approve → approved + paid; deny → denied (second deny silent); failed payout; repeated polling of a submitted payout; replayed request id
- a raising hook gives byte-identical `process()` output to a quiet one (all three verdicts)
- a raising registered sink is logged and the payment still pays
- a sink blocked for 2 s does not delay `process()` (< 0.5 s)
- one sink mutating its payload does not change what the next sink sees or the stored row
- payload carries `event` and excludes internals such as `policy_snapshot`
- unknown event kinds are dropped; `install_from_env` is a no-op with no env set

## Gaps and follow-ups
- CI has not run yet: it runs on the first push.
- Doppler and the other Student Pack claims need you to sign up; nothing in code depends on them.
