"""Schema creation, migration idempotency, and FTS trigger behavior.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_db.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import EMBED_DIM, open_db
from sherpa.corpus.schema import SCHEMA_VERSION


def test_open_db_creates_schema_and_stamps_version(tmp_path):
    conn = open_db(tmp_path / "c.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert {"messages", "chunks", "channels", "users", "sync_state"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_open_db_is_idempotent(tmp_path):
    path = tmp_path / "c.db"
    open_db(path).close()
    conn = open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_message_insert_is_searchable_via_fts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
        " ts, author_id, author_name, is_me, text, permalink)"
        " VALUES ('w:c:1', 'w', 'C1', 'gravi-cli', 'public', 1.0, 'U1', 'Jim Vogel', 1,"
        " 'I will have the schema thursday', 'https://x')"
    )
    conn.commit()
    hits = conn.execute(
        "SELECT id FROM messages_fts WHERE messages_fts MATCH 'schema'"
    ).fetchall()
    assert [row[0] for row in hits] == ["w:c:1"]


def test_message_delete_removes_it_from_fts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
        " ts, author_id, author_name, is_me, text, permalink)"
        " VALUES ('w:c:1', 'w', 'C1', 'gravi-cli', 'public', 1.0, 'U1', 'Jim', 1,"
        " 'tombstone me', 'https://x')"
    )
    conn.execute("DELETE FROM messages WHERE id = 'w:c:1'")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'tombstone'"
    ).fetchone()[0] == 0


def test_vec_table_accepts_embed_dim_vectors(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash)"
        " VALUES (1, 'w', 'C1', 1.0, 2.0, 'Jim', 'w:c:1', 'w:c:2', 'hdr', 'body', 'h')"
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (1, ?)",
        (bytes(4 * EMBED_DIM),),
    )
    conn.commit()
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1


def test_migration_is_crash_safe(tmp_path):
    """Verify that forced version reset does not break re-open (tests idempotent DDL)."""
    path = tmp_path / "c.db"
    conn = open_db(path)
    conn.close()

    # Simulate crash: reset user_version to 0 while tables exist
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    # This should succeed with idempotent DDL (IF NOT EXISTS)
    conn = open_db(path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert {"messages", "chunks", "channels", "users", "sync_state"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
