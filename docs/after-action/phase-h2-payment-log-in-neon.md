# After-action: hackathon P2, payment log in Neon

## Why

Render's free plan wipes the disk on every deploy and restart. So
`leash.db` (decisions, price events) and `payouts.db` (the records that stop a
payout being sent twice) disappeared each time. Only the limits were already
in Neon.

## What was built

**`src/leash/db.py`, one database layer**:
- **`SqliteDatabase`** works exactly as before.
- **`PostgresDatabase`**:
  - **Connections:** a small pool of kept connections, up to 4 idle. A pool,
    not one shared connection, because the gate sometimes opens a second
    connection while holding the first, as it already did with SQLite.
  - **Separation:** each copy works in its own schema
    (`leash_<settings namespace>`, made safe by `schema_name`). The schema is
    set with `SET LOCAL` per transaction, which works through Neon's pooler.
  - **Setup:** the schema is created over the direct URL, as Neon recommends
    for schema changes.
  - **Stale connections:** if the first statement on a kept connection fails
    (Neon closed it while idle), a new connection is used. Nothing has run
    yet at that point, so the retry is safe.
- **`translate()`** rewrites the five SQLite-only forms Leash uses, and the
  rest of the SQL is unchanged:

  | SQLite | Postgres |
  |---|---|
  | `json_extract(col, '$.a.b')` | `(col::jsonb #>> '{a,b}')` |
  | `IS NOT ?` | `IS DISTINCT FROM %s` |
  | `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` |
  | `rowid` | `seq` (BIGSERIAL column) |
  | `?` and `%` | `%s` and `%%` |

  `BEGIN IMMEDIATE` becomes `begin_exclusive()`. On Postgres that's
  `LOCK TABLE decisions IN SHARE ROW EXCLUSIVE MODE`: one writer at a time for
  the check-then-write steps (reserve, approve claim), with reads unaffected.
- **`database_from_env()`** uses the same URL variables and namespace as the
  limits.

**Gate and PayPal client changes:**
- **`gate.py`:** a `database=` parameter, and a Postgres version of the
  tables.
- **`_state()`:** no longer opens a raw `sqlite3` connection.
- **Price-alert query:** `CAST(? AS TEXT) IS NULL`, because Postgres can't
  guess the type of a bare NULL parameter.
- **`PayPalClient`:** takes `database=`. When the gate is on Postgres, its own
  client keeps payout records there too.

**Only the dashboard and MCP server use Neon** (`make_gate`, `_get_gate`).
Demos (`demo.py`, `demo_injection.py`) and tests keep local files, so a
scripted run never lands in the real log.

## Checks

- **SQLite:** full suite, 337 passed, 8 skipped. The 7 Postgres tests skip
  without a test URL.
- **Postgres:** `tests/test_db_postgres.py` on a new Neon branch
  `leash-tests` (`br-round-frog-bfzywm7w`, its own endpoint, checked to differ
  from production). All 13 pass: 6 translation tests and 7 on the database.
  - decisions survive a restart, newest first, and the spend is correct;
  - a repeated request ID makes one decision;
  - 5 approvals at once pay once;
  - 12 concurrent $8 requests against a $50 budget pay exactly 6;
  - a payout PayPal never answered is resent and followed to paid;
  - price events are stored once and show on the waiting request;
  - payout records survive a restart, and the payout is never sent twice.
- **Speed:** slow from Nairobi to Neon in Oregon (about 3 minutes for the
  file, mostly connection setup). Render is in Oregon next to the database,
  so the hosted dashboard should be fast.

## Found and fixed while testing

- **Postgres refused `? IS NULL` with a NULL value** ("could not determine
  data type"), in the price-alert query; fixed with the `CAST` above.
- **One of my tests was wrong:** $8 + $45 against a $50 budget is correctly
  blocked. Leash was right; the test now uses $41.

## Left for you

- **Test branch:** `leash-tests` is still in your Neon project. I didn't
  delete it. Delete it in Neon when you like, or keep it for the next run.
- **Laptop:** your `.env` has `DATABASE_URL`, so a local dashboard now logs to
  Neon too, in the `leash_local` schema, separate from Render's. Start it with
  `DATABASE_URL=` to keep it local.
- **Old Render data:** the payment log from before this deploy was on
  Render's disk and is gone. The log starts fresh in Neon from this deploy.
