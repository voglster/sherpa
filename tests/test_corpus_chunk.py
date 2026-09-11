"""Conversation windowing: gaps, thread isolation, overlap, script format, headers.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_chunk.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.chunk import (
    GAP_SECONDS,
    MAX_MESSAGES,
    build_chunks,
    build_header,
    format_script,
)
from sherpa.corpus.db import open_db


def insert(conn, ts, author, text, *, thread_ts=None, channel="C1", kind="public", name="gravi-cli"):
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " thread_ts, author_id, author_name, is_me, text, permalink)"
        " VALUES (?, 'w', ?, ?, ?, ?, ?, 'U', ?, 0, ?, 'https://x')",
        (f"w:{channel}:{ts}", channel, name, kind, ts, thread_ts, author, text),
    )


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "c.db")


def test_messages_within_the_gap_land_in_one_chunk(conn):
    for i in range(5):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 1


def test_a_silence_longer_than_the_gap_starts_a_new_chunk(conn):
    insert(conn, 1000.0, "Jim Vogel", "before")
    insert(conn, 1000.0 + GAP_SECONDS + 1, "Jim Vogel", "after")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 2


def test_a_long_run_splits_at_the_message_cap(conn):
    for i in range(MAX_MESSAGES + 5):
        insert(conn, 1000.0 + i, "Jim Vogel", f"m{i}")
    conn.commit()
    assert build_chunks(conn, "w", "C1") == 2


def test_adjacent_chunks_overlap_so_boundary_commitments_appear_twice(conn):
    insert(conn, 1000.0, "Jim Vogel", "unique_early")
    insert(conn, 1000.0 + 60, "Jim Vogel", "boundary_promise")
    insert(conn, 1000.0 + GAP_SECONDS + 61, "Jim Vogel", "unique_late")
    conn.commit()
    build_chunks(conn, "w", "C1")
    texts = [row[0] for row in conn.execute("SELECT text FROM chunks ORDER BY start_ts")]
    assert sum("boundary_promise" in t for t in texts) == 2


def test_a_thread_becomes_its_own_chunk(conn):
    insert(conn, 1000.0, "Jim Vogel", "channel chatter")
    insert(conn, 1001.0, "Hunter Reeves", "thread parent", thread_ts=1001.0)
    insert(conn, 1002.0, "Jim Vogel", "thread reply", thread_ts=1001.0)
    conn.commit()
    build_chunks(conn, "w", "C1")
    thread_chunks = conn.execute("SELECT text FROM chunks WHERE thread_ts IS NOT NULL").fetchall()
    assert len(thread_chunks) == 1
    assert "thread reply" in thread_chunks[0][0]
    assert "channel chatter" not in thread_chunks[0][0]


def test_script_format_names_each_speaker(conn):
    rows = [
        {"author_name": "Jim Vogel", "text": "I'll have it thursday"},
        {"author_name": "Hunter Reeves", "text": "perfect"},
    ]
    assert format_script(rows) == "Jim Vogel: I'll have it thursday\nHunter Reeves: perfect"


def test_header_describes_a_dm_with_date_and_participants():
    header = build_header("Hunter Reeves", "im", ["Jim Vogel", "Hunter Reeves"], 1755129600.0)
    assert "DM" in header
    assert "Hunter Reeves" in header
    assert "2025-08" in header or "2025-08" in header


def test_header_describes_a_channel_with_its_name():
    header = build_header("gravi-cli", "public", ["Jim Vogel"], 1755129600.0)
    assert "#gravi-cli" in header


def test_rebuilding_a_channel_replaces_rather_than_duplicates(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    build_chunks(conn, "w", "C1")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_unchanged_chunk_keeps_its_embedded_flag(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    conn.execute("UPDATE chunks SET embedded = 1")
    conn.commit()
    build_chunks(conn, "w", "C1")
    assert conn.execute("SELECT embedded FROM chunks").fetchone()[0] == 1


def test_extending_a_still_open_window_replaces_the_stale_chunk_not_duplicates_it(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    old_id, old_last = conn.execute("SELECT id, msg_last_id FROM chunks").fetchone()

    insert(conn, 1000.0 + 3 * 60, "Jim Vogel", "m3")
    conn.commit()
    count = build_chunks(conn, "w", "C1")

    rows = conn.execute("SELECT id, msg_first_id, msg_last_id FROM chunks").fetchall()
    assert count == 1
    assert len(rows) == 1
    assert rows[0]["msg_first_id"] == "w:C1:1000.0"
    assert rows[0]["msg_last_id"] == "w:C1:1180.0"
    assert rows[0]["msg_last_id"] != old_last
    assert conn.execute(
        "SELECT count(*) FROM chunks WHERE id = ?", (old_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunk_id = ?", (old_id,)
    ).fetchone()[0] == 0


def test_rebuild_removes_the_vector_row_of_a_replaced_chunk(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    old_id = conn.execute("SELECT id FROM chunks").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
        (old_id, b"\x00" * (768 * 4)),
    )
    conn.commit()

    insert(conn, 1000.0 + 3 * 60, "Jim Vogel", "m3")
    conn.commit()
    build_chunks(conn, "w", "C1")

    assert conn.execute(
        "SELECT count(*) FROM chunks_vec WHERE chunk_id = ?", (old_id,)
    ).fetchone()[0] == 0
