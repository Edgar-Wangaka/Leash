"""Where Leash keeps its payment log: a local SQLite file, or a hosted Postgres.

Render's free plan wipes the disk on every deploy and restart, so a hosted
copy keeps decisions, payout records and price events in Postgres (Neon). The
SQL is written for SQLite; ``translate`` rewrites the few parts Postgres
spells differently. Each copy of Leash works in its own Postgres schema,
named like its settings namespace, so a laptop and the Render deploy sharing
one database never see each other's payments.

Only the dashboard and the MCP server pass a database URL. Demos and tests
always use a local file, so a scripted run never lands in the real log.
"""

from __future__ import annotations

import queue
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator


def translate(sql: str) -> str:
    """SQLite SQL as Postgres expects it. Only the forms Leash uses."""
    sql = re.sub(
        r"json_extract\(\s*(\w+)\s*,\s*'\$\.([\w.]+)'\s*\)",
        lambda m: f"({m.group(1)}::jsonb #>> '{{{m.group(2).replace('.', ',')}}}')",
        sql,
    )
    sql = re.sub(r"\bIS NOT \?", "IS DISTINCT FROM ?", sql)
    if re.match(r"\s*INSERT OR IGNORE INTO", sql, re.I):
        sql = re.sub(r"INSERT OR IGNORE INTO", "INSERT INTO", sql, count=1, flags=re.I)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = re.sub(r"\browid\b", "seq", sql)
    return sql.replace("%", "%%").replace("?", "%s")


def schema_name(namespace: str) -> str:
    """A safe Postgres schema name for a namespace such as a host name."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", namespace.lower()).strip("_") or "local"
    return f"leash_{cleaned}"[:60]


class SqliteDatabase:
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def connect(self, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        """A connection that commits (or rolls back) and then closes.

        ``with sqlite3.connect(...)`` alone only commits; the connection stays
        open until garbage collection, and a dashboard polling every 1.5 s
        piles up open database handles.
        """
        connection = sqlite3.connect(self.path, timeout=timeout)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin_exclusive(self, connection: sqlite3.Connection, table: str) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def close(self) -> None:
        pass


class _PgConnection:
    """A psycopg connection that takes SQLite SQL and stays in Leash's schema."""

    def __init__(self, raw: Any, schema: str) -> None:
        self.raw = raw
        self.schema = schema

    def _set_path(self) -> None:
        # SET LOCAL lasts one transaction, which is all Neon's pooler
        # (PgBouncer in transaction mode) keeps for a client anyway.
        self.raw.execute(f'SET LOCAL search_path TO "{self.schema}"')

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        return self.raw.execute(translate(sql), tuple(params))

    def commit(self) -> None:
        self.raw.commit()
        self._set_path()

    def rollback(self) -> None:
        self.raw.rollback()
        self._set_path()


class PostgresDatabase:
    """A few kept connections to Postgres, each used by one caller at a time.

    The gate sometimes opens a second connection while holding one (to read a
    record it just wrote), as it does with SQLite, so this is a small pool
    rather than one shared connection.
    """

    dialect = "postgres"
    POOL_SIZE = 4

    def __init__(self, dsn: str, namespace: str, *, migrate_dsn: str | None = None,
                 connect=None) -> None:
        self.dsn = dsn
        self.migrate_dsn = migrate_dsn or dsn
        self.schema = schema_name(namespace)
        self._connect_fn = connect
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._schema_ready = False

    def _new(self, dsn: str) -> Any:
        if self._connect_fn is not None:
            return self._connect_fn(dsn)
        import psycopg  # the optional "postgres" extra

        # prepare_threshold=None: no server-side prepared statements, which
        # PgBouncer in transaction mode (Neon's pooler) does not support.
        return psycopg.connect(dsn, connect_timeout=10, autocommit=False, prepare_threshold=None)

    def ensure_schema(self) -> None:
        """Create Leash's schema once, over the direct (unpooled) URL."""
        with self._lock:
            if self._schema_ready:
                return
            raw = self._new(self.migrate_dsn)
            try:
                raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                raw.commit()
            finally:
                raw.close()
            self._schema_ready = True

    def _checkout(self) -> Any:
        while True:
            try:
                raw = self._idle.get_nowait()
            except queue.Empty:
                return self._new(self.dsn)
            if not getattr(raw, "closed", False):
                return raw

    def _checkin(self, raw: Any) -> None:
        if getattr(raw, "closed", False) or self._idle.qsize() >= self.POOL_SIZE:
            try:
                raw.close()
            except Exception:
                pass
            return
        self._idle.put(raw)

    @contextmanager
    def connect(self, timeout: float = 5.0) -> Iterator[_PgConnection]:
        self.ensure_schema()
        raw = self._checkout()
        connection = _PgConnection(raw, self.schema)
        try:
            # Neon closes idle connections. If the very first statement fails,
            # nothing has happened yet, so a fresh connection is safe to use.
            try:
                connection._set_path()
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass
                raw = self._new(self.dsn)
                connection = _PgConnection(raw, self.schema)
                connection._set_path()
            yield connection
            raw.commit()
        except BaseException:
            try:
                raw.rollback()
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass
            raise
        finally:
            self._checkin(raw)

    def begin_exclusive(self, connection: _PgConnection, table: str) -> None:
        # The same guarantee as SQLite's BEGIN IMMEDIATE for a check-then-write:
        # one writer at a time; plain reads still go ahead.
        connection.raw.execute(f'LOCK TABLE "{table}" IN SHARE ROW EXCLUSIVE MODE')

    def close(self) -> None:
        while True:
            try:
                raw = self._idle.get_nowait()
            except queue.Empty:
                return
            try:
                raw.close()
            except Exception:
                pass


Database = SqliteDatabase | PostgresDatabase


def database_from_env(path: str) -> Database:
    """Postgres when a database URL is set (the same one the limits use), else ``path``.

    Only the dashboard and the MCP server call this; see the module docstring.
    """
    from . import settings  # local import: settings imports nothing from here

    dsn, migrate_dsn, names = settings.database_urls()
    if not dsn:
        return SqliteDatabase(path)
    try:
        import psycopg  # noqa: F401
    except ImportError as error:
        raise SystemExit(
            'A database URL is set but psycopg is missing: pip install ".[postgres]"'
        ) from error
    try:
        return PostgresDatabase(
            settings.require_tls(dsn, names[0]),
            settings.default_namespace(),
            migrate_dsn=settings.require_tls(migrate_dsn, names[1]) if migrate_dsn else None,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
