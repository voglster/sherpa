"""Opening the corpus database: extension load, migration, pragmas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from sherpa.corpus.schema import EMBED_DIM, MIGRATIONS, SCHEMA_VERSION

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

    if vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    _migrate(conn, vec=vec)
    return conn


def _migrate(conn: sqlite3.Connection, *, vec: bool) -> None:
    applied = conn.execute("PRAGMA user_version").fetchone()[0]
    for statement in MIGRATIONS[applied:]:
        conn.executescript(statement)
    if vec:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])"
        )
    if applied != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
