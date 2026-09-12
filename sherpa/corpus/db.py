"""Opening the corpus database: extension load, migration, pragmas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from sherpa.corpus.schema import COLUMN_ADDITIONS, EMBED_DIM, LATE_INDEXES, MIGRATIONS, SCHEMA_VERSION

__all__ = ["EMBED_DIM", "SCHEMA_VERSION", "open_db"]


def open_db(path: Path, *, vec: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the corpus database, migrated to SCHEMA_VERSION."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    # WAL lets readers run against a live sync; this covers the brief moments
    # of genuine contention (a checkpoint, a schema change) so they wait rather
    # than failing instantly.
    conn.execute("PRAGMA busy_timeout = 5000")

    if vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    _migrate(conn, vec=vec)
    return conn


def _migrate(conn: sqlite3.Connection, *, vec: bool) -> None:
    applied = conn.execute("PRAGMA user_version").fetchone()[0]
    for idx, statement in enumerate(MIGRATIONS[applied:], start=applied + 1):
        conn.executescript(statement + f"\nPRAGMA user_version = {idx};")
    _add_columns(conn)
    for statement in LATE_INDEXES:
        conn.execute(statement)
    if vec:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])"
        )
    conn.commit()


def _add_columns(conn: sqlite3.Connection) -> None:
    """Apply COLUMN_ADDITIONS to whichever tables exist, exactly once each.

    `ADD COLUMN` has no idempotent form, so the guard is an explicit look at
    what the table already has. Nothing is written -- not even a transaction is
    opened -- unless something is genuinely missing, which is every open after
    the first: every read command opens the database, and taking a write lock
    to discover there is no work makes `status` and `search` fail outright
    while a sync holds the lock.
    """
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    pending = []
    for table, column, declaration, backfill in COLUMN_ADDITIONS:
        if table not in tables:
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            pending.append((table, column, declaration, backfill))
    if not pending:
        return

    # One transaction around both halves. sqlite3 runs DDL in autocommit and
    # opens a separate transaction for the following UPDATE, so a crash between
    # them would leave the column present but unfilled -- and "does the column
    # exist" is the only guard, so the backfill would never run again. The
    # column would then read empty forever, silently, which for thread_ts_raw
    # means the whole sweep path goes quiet.
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, column, declaration, backfill in pending:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            if backfill:
                conn.execute(backfill)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
