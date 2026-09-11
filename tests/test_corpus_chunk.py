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


def test_a_no_op_rebuild_returns_the_stored_count_not_zero(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}")
    conn.commit()
    build_chunks(conn, "w", "C1")
    assert build_chunks(conn, "w", "C1") == 1


def test_delete_stale_is_scoped_to_its_own_workspace_and_channel(conn):
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}", channel="C1")
    for i in range(3):
        insert(conn, 1000.0 + i * 60, "Jim Vogel", f"m{i}", channel="C2")
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " thread_ts, author_id, author_name, is_me, text, permalink)"
        " VALUES ('w2:C1:1000.0', 'w2', 'C1', 'gravi-cli', 'public', 1000.0, NULL, 'U',"
        " 'Jim Vogel', 0, 'other workspace', 'https://x')"
    )
    conn.commit()

    build_chunks(conn, "w", "C1")
    build_chunks(conn, "w", "C2")
    build_chunks(conn, "w2", "C1")

    c2_id = conn.execute(
        "SELECT id FROM chunks WHERE workspace = 'w' AND channel_id = 'C2'"
    ).fetchone()[0]
    other_ws_id = conn.execute(
        "SELECT id FROM chunks WHERE workspace = 'w2' AND channel_id = 'C1'"
    ).fetchone()[0]
    for chunk_id in (c2_id, other_ws_id):
        conn.execute(
            "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, b"\x00" * (768 * 4)),
        )
    conn.commit()

    insert(conn, 1000.0 + 3 * 60, "Jim Vogel", "m3", channel="C1")
    conn.commit()
    build_chunks(conn, "w", "C1")

    assert conn.execute(
        "SELECT count(*) FROM chunks WHERE workspace = 'w' AND channel_id = 'C2'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks WHERE workspace = 'w2' AND channel_id = 'C1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunk_id = ?", (c2_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunk_id = ?", (other_ws_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks_vec WHERE chunk_id = ?", (c2_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks_vec WHERE chunk_id = ?", (other_ws_id,)
    ).fetchone()[0] == 1


# --- Who the conversation is with, not who happened to speak ----------------
#
# An outstanding promise is one-sided by construction: Jim spoke and the other
# person has not answered yet. Sourcing participants from the speakers in the
# window makes exactly that chunk invisible to `owed hunter` and strips the
# other party out of its header, which is the one line Contextual Retrieval
# embeds and indexes.

def _dm_channel(conn, channel="D9", name="Hunter Reeves"):
    conn.execute(
        "INSERT INTO users (workspace, id, name, is_me) VALUES ('w', 'U_ME', 'Jim Vogel', 1)"
    )
    conn.execute("INSERT INTO users (workspace, id, name) VALUES ('w', 'U_H', ?)", (name,))
    conn.execute(
        "INSERT INTO channels (workspace, id, name, kind, is_member, member_ids)"
        " VALUES ('w', ?, ?, 'im', 1, '[\"U_ME\", \"U_H\"]')",
        (channel, name),
    )


def test_a_one_sided_dm_chunk_still_names_the_other_party(conn):
    _dm_channel(conn)
    insert(conn, 1757606400.000100, "Jim Vogel", "I'll get you the schema thursday",
           channel="D9", kind="im", name="Hunter Reeves")
    conn.commit()
    build_chunks(conn, "w", "D9")

    row = conn.execute("SELECT header, people FROM chunks").fetchone()
    assert "Hunter Reeves" in row["people"]
    assert "Jim Vogel" in row["people"]
    assert "Hunter Reeves" in row["header"]
    assert row["header"].startswith("DM between Jim Vogel and Hunter Reeves")


def test_channel_membership_reaches_the_fts_index_too(conn):
    _dm_channel(conn)
    insert(conn, 1757606400.000100, "Jim Vogel", "I'll get you the schema thursday",
           channel="D9", kind="im", name="Hunter Reeves")
    conn.commit()
    build_chunks(conn, "w", "D9")

    assert conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '\"Hunter\"'"
    ).fetchone()[0] == 1


def test_the_fts_row_is_addressable_by_rowid_not_a_full_scan(conn):
    """`chunks_fts.chunk_id` is UNINDEXED, so deleting by it scans the whole
    index -- once per changed window, over 50k chunks, inside a cron run's
    wall-clock budget. Keying the fts row by the chunk's own id makes it a
    lookup instead."""
    insert(conn, 1757606400.000100, "Jim Vogel", "hello")
    insert(conn, 1757610000.000100, "Jim Vogel", "much later")
    conn.commit()
    build_chunks(conn, "w", "C1")

    for chunk_id in [row[0] for row in conn.execute("SELECT id FROM chunks")]:
        assert conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE rowid = ?", (chunk_id,)
        ).fetchone()[0] == chunk_id


def test_an_older_database_has_its_fts_rowids_repaired_on_open(tmp_path):
    """Rows written before the rowid keying took an autoincremented rowid of
    their own, so a delete by rowid would miss them and leave the index holding
    a chunk that no longer exists."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = open_db(path)
    conn.execute(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash)"
        " VALUES (5, 'w', 'C1', 1.0, 2.0, 'Jim Vogel', 'a', 'b', 'hdr', 'body', 'h')"
    )
    conn.execute("DELETE FROM chunks_fts")
    conn.execute(
        "INSERT INTO chunks_fts (rowid, chunk_id, header, text, participants)"
        " VALUES (100, 5, 'hdr', 'body', 'Jim Vogel')"
    )
    conn.commit()
    conn.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    conn = open_db(path)
    assert conn.execute(
        "SELECT chunk_id FROM chunks_fts WHERE rowid = 5"
    ).fetchone()[0] == 5
    assert conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1
