"""Hybrid retrieval: filters before fusion, RRF, hydration, dedupe.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_search.py -v
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.chunk import build_chunks
from sherpa.corpus.db import EMBED_DIM, open_db
from sherpa.corpus.embed import FakeEmbedder, embed_pending
from sherpa.corpus.search import (
    Filters,
    KNN_DISTANCE_FLOOR,
    RRF_K,
    conversation_with,
    search,
    sent_to,
    thread_at,
)


class ScriptedEmbedder:
    """Vectors at chosen, exact distances -- unlike FakeEmbedder's hash-derived
    vectors, which are noise uncorrelated with meaning and can't exercise a
    distance floor at all. Maps exact input strings to explicit vectors, with
    everything else (chunk text, by default) landing at `default`.
    """

    def __init__(self, vectors: dict[str, list[float]], default: list[float]):
        self._vectors = vectors
        self._default = default

    def embed(self, texts):
        return [self._vectors.get(t, self._default) for t in texts]


def _vector_at_distance(distance: float) -> list[float]:
    """A vector whose L2 distance from an all-zero vector is exactly `distance`."""
    return [float(distance)] + [0.0] * (EMBED_DIM - 1)


FLOOR_ORIGIN = [0.0] * EMBED_DIM
FLOOR_FAR = _vector_at_distance(KNN_DISTANCE_FLOOR + 5.0)
FLOOR_NEAR = _vector_at_distance(KNN_DISTANCE_FLOOR / 2)


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


def test_overlapping_hits_are_merged_not_returned_twice(tmp_path):
    """Chunk windows overlap by design: a channel long enough to split
    (chunk.MAX_MESSAGES) carries chunk.OVERLAP messages into the next window,
    so two chunks can share messages while starting at different ones. A bare
    `start_ts` equality check misses this -- it only catches two hits that
    happen to start at the identical message, not two that genuinely overlap.
    This fixture forces a real overlap: 42 closely-spaced messages in one
    channel split into two windows sharing messages 38-39, both matching the
    query, so a correct implementation must recognize the overlap and return
    only one hit. Do not shrink this fixture -- a handful of messages can't
    trigger the MAX_MESSAGES split this test depends on.
    """
    conn = open_db(tmp_path / "overlap.db")
    for i in range(42):
        text = "shared_marker filler" if i in (38, 39) else f"filler message {i}"
        insert(conn, 3000.0 + i * 10, "Hunter Reeves", text, channel="DOVERLAP")
    conn.commit()
    build_chunks(conn, "gravitate", "DOVERLAP")
    embed_pending(conn, FakeEmbedder())

    hits = search(conn, "shared_marker", FakeEmbedder(), filters=Filters(channel="DOVERLAP"), k=10)
    assert len(hits) == 1


def test_hard_filter_finds_true_nearest_within_filter_not_global_topn(tmp_path):
    """A global-KNN-then-intersect implementation looks correct on a tiny
    fixture only because the whole corpus fits inside the global top-k. At
    real scale it silently fails: the true nearest neighbours *within* a
    filtered conversation routinely rank outside the global top-k, so a
    `--from hunter` query with a real semantic match inside Hunter's
    conversation returns nothing, because that match never reaches the
    global candidate window that gets intersected with the filter afterward.

    This fixture makes the two behaviors diverge on purpose: thousands of
    background chunks are deliberately placed CLOSER to the query vector than
    the one real, filtered-in chunk. A global top-CANDIDATES KNN never reaches
    the real chunk; a genuinely pre-filtered KNN (searching only within the
    allowed set) finds it immediately, since it is the only candidate.
    BACKGROUND_CHUNK_COUNT must stay large enough that CANDIDATES (50) cannot
    cover it -- do not shrink this fixture "to make it faster"; a small one
    cannot express this bug at all.
    """
    BACKGROUND_CHUNK_COUNT = 6000
    conn = open_db(tmp_path / "scale.db")
    dim = EMBED_DIM

    query_vector = [0.0] * dim
    background_vector = struct.pack(f"{dim}f", *_vector_at_distance(0.05))

    conn.executemany(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, 'gravitate', 'noise', 0, 0, 'Nobody', ?, ?, 'h', 't', ?, 1)",
        [(i, f"x{i}a", f"x{i}b", f"hash{i}") for i in range(1, BACKGROUND_CHUNK_COUNT + 1)],
    )
    conn.executemany(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
        [(i, background_vector) for i in range(1, BACKGROUND_CHUNK_COUNT + 1)],
    )

    insert(conn, 1000.0, "Hunter Reeves", "the real answer is monday", channel="DHUNTER")
    insert(conn, 1010.0, "Jim Vogel", "great, thanks", is_me=1, channel="DHUNTER")
    conn.commit()
    build_chunks(conn, "gravitate", "DHUNTER")
    allowed_chunk_id = conn.execute(
        "SELECT id FROM chunks WHERE channel_id = 'DHUNTER'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
        (allowed_chunk_id, struct.pack(f"{dim}f", *_vector_at_distance(0.5))),
    )
    conn.execute("UPDATE chunks SET embedded = 1 WHERE id = ?", (allowed_chunk_id,))
    conn.commit()

    query = "distinctive_query_term_not_in_corpus"
    embedder = ScriptedEmbedder({query: query_vector}, _vector_at_distance(0.05))
    hits = search(conn, query, embedder, filters=Filters(person="Hunter Reeves"), k=5)
    assert any("the real answer is monday" in hit.transcript for hit in hits)


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


@pytest.fixture
def floor_corpus(tmp_path):
    conn = open_db(tmp_path / "floor.db")
    insert(conn, 1000.0, "Hunter Reeves", "completely unrelated gardening chatter", channel="DF")
    insert(conn, 1060.0, "Jim Vogel", "yep the KB-77777 patch is in review", is_me=1, channel="DF")
    conn.commit()
    build_chunks(conn, "gravitate", "DF")
    embed_pending(conn, ScriptedEmbedder({}, FLOOR_ORIGIN))
    return conn


def test_knn_only_hit_beyond_the_floor_is_dropped(floor_corpus):
    """No BM25 match, and the only vector candidate is well past the floor: []."""
    embedder = ScriptedEmbedder({"nothing_like_this_corpus": FLOOR_FAR}, FLOOR_ORIGIN)
    assert search(floor_corpus, "nothing_like_this_corpus", embedder, filters=Filters(), k=5) == []


def test_knn_only_hit_within_the_floor_is_returned(floor_corpus):
    """No BM25 match, but the vector candidate clears the floor: it counts."""
    embedder = ScriptedEmbedder({"nothing_like_this_corpus": FLOOR_NEAR}, FLOOR_ORIGIN)
    hits = search(floor_corpus, "nothing_like_this_corpus", embedder, filters=Filters(), k=5)
    assert hits


def test_bm25_hit_survives_the_floor_regardless_of_distance(floor_corpus):
    """An exact-token hit (a Jira key) must never be suppressed by the vector floor."""
    embedder = ScriptedEmbedder({"KB-77777": FLOOR_FAR}, FLOOR_ORIGIN)
    hits = search(floor_corpus, "KB-77777", embedder, filters=Filters(), k=5)
    assert any("KB-77777" in hit.transcript for hit in hits)


def test_rrf_k_is_the_standard_constant():
    assert RRF_K == 60
