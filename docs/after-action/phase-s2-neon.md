# After-action report: user-set limits, phase S2 (Neon Postgres)

**Date:** 2026-11-11 · **Result:** done locally; the hosted copy needs three Render settings from you · **Tests:** 233 passed, plus 1 real Neon round trip (run with `LEASH_TEST_POSTGRES_URL`)

## What was planned
Keep the human's limit changes in a free hosted Postgres (Neon), so they survive Render's free-plan restarts, without changing how the gate decides.

## What was done
| Item | Where |
|---|---|
| `PostgresSettingsStore`: the same versions table as S1 (`leash_policy_settings`), scoped by a `namespace` column, all queries parameterised | `settings.py` |
| Namespace: `LEASH_SETTINGS_NAMESPACE`, else the first `LEASH_PUBLIC_HOST`, else `local`, so the laptop and Render never share limits by accident | `default_namespace` |
| URLs: `LEASH_SETTINGS_DATABASE_URL` if set, else Neon's `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED` (direct), as `neon env pull` writes them | `database_urls` |
| Per the Neon Postgres skill: queries over the pooled URL with `prepare_threshold=None` (PgBouncer transaction mode), table creation over the direct URL | `_connection`, `_ensure_table` |
| TLS required for any non-local host; `sslmode=require` added when missing, `disable`/`allow`/`prefer` refused | `require_tls` |
| One kept connection; a read that fails (Neon closes idle connections and suspends an idle database after 5 minutes) is retried once on a fresh connection; a write is never retried, because it may have committed | `_run` |
| Slow stores refresh in the background: only the first read waits, then decisions use the cached copy and a stale one triggers a single background refresh. A save updates the cache directly | `LiveSettings.current`, `change` |
| URL set but `psycopg` missing, or an unsafe URL: Leash refuses to start | `store_from_env` |
| `postgres` extra (`psycopg[binary]>=3.1`); `render.yaml` build and env entries | `pyproject.toml`, `render.yaml` |
| Tests never touch a real database: `conftest.py` blanks all four URL variables (an empty value beats `.env`) | `tests/conftest.py` |
| Neon project linked with `neon init` (`neon.ts`, `.neon`, `package.json`); `.env.local` added to `.gitignore` before Neon wrote any env file | repo root |

## Real runs (Neon project `lingering-meadow-20776765`, aws-us-west-2)
- **Round trip test:** passed. It saves a change, reads it back through a fresh store as after a restart, and deletes its namespace afterwards.
- **Survives a restart:** a throwaway dashboard in namespace `e2e-test` changed the budget to $175. It was killed, and a fresh process with an empty `leash.db` still showed $175, version 3. Test rows deleted; the table is empty.
- **Latency from Nairobi to Oregon:** a fresh connection took 2.6–6.6 s per read before the fix. With the kept connection: first read 5.8 s, then about 0.9 s per query. Gate reads after warm-up: 100 in 1 ms. Render is in `oregon`, next to the database, so hosted reads should be milliseconds.

## Problems hit
- **Every read opened a new TLS connection** (several seconds from Kenya), and the gate would have waited on it before decisions. Fixed with the kept connection and background refresh.
- **Neon wrote quoted values** to `.env`. Leash's loader strips quotes; only my shell export didn't, which made the first real test fail on a "not a postgres:// URL" error.
- **`neon deploy` pulled variables into `.env`, not `.env.local`.** That's what Leash reads. `.env.local` is ignored anyway.
- **A test fake hid connection reuse:** its connect wrapper never reset the "closed" flag. Fixed so the test checks one direct then one kept pooled connection.
- **Installing the Neon CLI globally** was blocked for me once; it ran in a terminal tab after you asked again.

## Decisions
- Background refresh means another process (for example the local MCP server) can use limits up to about 2 seconds plus one network round trip old after a change elsewhere. On Render the dashboard and MCP endpoint share one process, so a change applies there at once.
- `neon init -y` also installed the Neon plugin for Claude Desktop, VS Code and Copilot CLI, and added `package.json` with `@neon/config` and `@neon/env`. Render's Python build ignores them.

## Not done (needs you)
1. **Render Build Command** → `pip install ".[remote,postgres]"`. Do this first: with a URL but no driver, Leash refuses to start.
2. **Render env:** `DATABASE_URL` and `DATABASE_URL_UNPOOLED` from Neon (secrets), and `LEASH_SETTINGS_NAMESPACE=render`.
3. **The extra Neon project** `little-boat-92260837` (also named `leash`, made before your setup steps) is unused. Delete it, or say so and I will.
4. **S3:** the dashboard form.
