"""Hybrid retrieval: filters before fusion, RRF, hydration, dedupe.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_search.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.chunk import build_chunks
from sherpa.corpus.db import open_db
from sherpa.corpus.embed import FakeEmbedder, embed_pending
from sherpa.corpus.search import (
    Filters,
    RRF_K,
    conversation_with,
    search,
    sent_to,
    thread_at,
)


def insert(conn, ts, author, text, *, is_me=0, channel="D1", kind="im", name="Hunter Reeves"):
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " author_id, author_name, is_me, text, permalink)"
        " VALUES (?, 'gravitate', ?, ?, ?, ?, 'U', ?, ?, ?, ?)",
        (
            f"gravitate:{channel}:{ts}",
            channel,
            name,
            kind,
            ts,
            author,
            is_me,
            text,
            f"https://gravitate.slack.com/archives/{channel}/p{int(ts)}",
        ),
    )


@pytest.fixture
def corpus(tmp_path):
    conn = open_db(tmp_path / "c.db")
    insert(conn, 1000.0, "Hunter Reeves", "can you take the gravi cli flag work?")
    insert(conn, 1060.0, "Jim Vogel", "yeah I'll have the schema to you thursday", is_me=1)
    insert(conn, 9000.0, "Erica Lin", "need the KB-51508 writeup", channel="D2", name="Erica Lin")
    insert(conn, 9060.0, "Jim Vogel", "I'll get it done monday", is_me=1, channel="D2", name="Erica Lin")
    conn.commit()
    build_chunks(conn, "gravitate", "D1")
    build_chunks(conn, "gravitate", "D2")
    embed_pending(conn, FakeEmbedder())
    return conn


def test_search_returns_hits_with_transcripts_and_permalinks(corpus):
    hits = search(corpus, "schema", FakeEmbedder(), filters=Filters(), k=5)
    assert hits
    assert "Jim Vogel:" in hits[0].transcript
    assert hits[0].permalink.startswith("https://gravitate.slack.com/archives/")


def test_exact_token_is_found_even_though_embeddings_are_fake(corpus):
    """BM25 carries the exact-token case; this is why retrieval is hybrid."""
    hits = search(corpus, "KB-51508", FakeEmbedder(), filters=Filters(), k=5)
    assert any("KB-51508" in hit.transcript for hit in hits)


def test_person_filter_excludes_other_conversations(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(person="Erica Lin"), k=5)
    assert hits
    assert all("Hunter" not in hit.transcript for hit in hits)


def test_mine_filter_requires_a_message_from_jim_in_the_chunk(corpus):
    corpus.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " author_id, author_name, is_me, text, permalink)"
        " VALUES ('gravitate:C9:1', 'gravitate', 'C9', 'noise', 'public', 500.0, 'U',"
        " 'Someone Else', 0, 'schema chatter nobody answered', 'https://x')"
    )
    corpus.commit()
    build_chunks(corpus, "gravitate", "C9")
    embed_pending(corpus, FakeEmbedder())
    hits = search(corpus, "schema", FakeEmbedder(), filters=Filters(mine=True), k=5)
    assert all("Someone Else" not in hit.transcript or "Jim Vogel" in hit.transcript for hit in hits)


def test_since_filter_excludes_older_conversations(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(since=5000.0), k=5)
    assert all(hit.start_ts >= 5000.0 for hit in hits)


def test_context_widens_the_hydrated_range(corpus):
    for i in range(6):
        insert(corpus, 2000.0 + i, "Hunter Reeves", f"filler{i}", channel="D3", name="Hunter Reeves")
    insert(corpus, 2010.0, "Jim Vogel", "distinctive_promise", is_me=1, channel="D3", name="Hunter Reeves")
    corpus.commit()
    build_chunks(corpus, "gravitate", "D3")
    embed_pending(corpus, FakeEmbedder())
    narrow = search(corpus, "distinctive_promise", FakeEmbedder(), filters=Filters(channel="D3"), k=1, context=0)
    wide = search(corpus, "distinctive_promise", FakeEmbedder(), filters=Filters(channel="D3"), k=1, context=3)
    assert len(wide[0].transcript) >= len(narrow[0].transcript)


def test_overlapping_hits_are_merged_not_returned_twice(corpus):
    hits = search(corpus, "I'll", FakeEmbedder(), filters=Filters(), k=10)
    starts = [hit.start_ts for hit in hits]
    assert len(starts) == len(set(starts))


def test_conversation_with_returns_both_directions_newest_first(corpus):
    hits = conversation_with(corpus, "Hunter Reeves")
    assert hits
    assert "Hunter Reeves:" in hits[0].transcript
    assert "Jim Vogel:" in hits[0].transcript


def test_sent_to_returns_only_chunks_containing_jims_messages(corpus):
    hits = sent_to(corpus, "Hunter Reeves")
    assert hits
    assert all("Jim Vogel:" in hit.transcript for hit in hits)


def test_thread_at_resolves_a_permalink_to_its_conversation(corpus):
    permalink = corpus.execute(
        "SELECT permalink FROM messages WHERE text LIKE 'yeah I%'"
    ).fetchone()[0]
    hit = thread_at(corpus, permalink)
    assert hit is not None
    assert "thursday" in hit.transcript


def test_empty_query_result_is_an_empty_list_not_an_error(corpus):
    assert search(corpus, "zzzznonexistent", FakeEmbedder(), filters=Filters(), k=5) == []


def test_rrf_k_is_the_standard_constant():
    assert RRF_K == 60
