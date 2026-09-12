"""Embedding backfill: batching, skip-when-embedded, loud failure.

Run: uv run --with pytest --with httpx --with sqlite-vec pytest tests/test_corpus_embed.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import EMBED_DIM, open_db
from sherpa.corpus.embed import (
    EMBED_RETRIES,
    EmbeddingError,
    FakeEmbedder,
    GatewayEmbedder,
    embed_pending,
)


def add_chunk(conn, chunk_id, text="hello", embedded=0):
    conn.execute(
        "INSERT INTO chunks (id, workspace, channel_id, start_ts, end_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, 'w', 'C1', 1.0, 2.0, 'Jim', ?, ?, 'hdr', ?, ?, ?)",
        (chunk_id, f"w:C1:{chunk_id}a", f"w:C1:{chunk_id}b", text, f"h{chunk_id}", embedded),
    )


def test_embed_pending_writes_vectors_and_flags_chunks(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    add_chunk(conn, 2)
    conn.commit()
    assert embed_pending(conn, FakeEmbedder()) == 2
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 0


def test_already_embedded_chunks_are_skipped(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1, embedded=1)
    conn.commit()
    assert embed_pending(conn, FakeEmbedder()) == 0


def test_embedding_text_includes_the_header(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1, text="body text")
    conn.commit()
    embedder = FakeEmbedder()
    embed_pending(conn, embedder)
    assert embedder.seen[0].startswith("hdr")
    assert "body text" in embedder.seen[0]


def test_batching_respects_the_batch_size(tmp_path):
    conn = open_db(tmp_path / "c.db")
    for i in range(5):
        add_chunk(conn, i + 1)
    conn.commit()
    embedder = FakeEmbedder()
    embed_pending(conn, embedder, batch=2)
    assert [len(b) for b in embedder.batches] == [2, 2, 1]


def test_gateway_failure_raises_rather_than_leaving_chunks_pending(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    conn.commit()

    class DeadEmbedder:
        def embed(self, texts):
            raise EmbeddingError("connection refused")

    with pytest.raises(EmbeddingError):
        embed_pending(conn, DeadEmbedder())
    assert conn.execute("SELECT embedded FROM chunks").fetchone()[0] == 0


def test_gateway_embedder_raises_embedding_error_on_http_failure():
    class DeadTransport:
        def post(self, url, headers=None, json=None):
            raise OSError("connection refused")

    embedder = GatewayEmbedder("https://llm.jc.turbo.inc/v1", "key", transport=DeadTransport())
    with pytest.raises(EmbeddingError):
        embedder.embed(["hello"])


def test_gateway_embedder_rejects_wrong_dimension():
    class ShortTransport:
        def post(self, url, headers=None, json=None):
            class R:
                status_code = 200

                def json(self):
                    return {"data": [{"embedding": [0.0, 1.0]}]}

            return R()

    embedder = GatewayEmbedder("https://llm.jc.turbo.inc/v1", "key", transport=ShortTransport())
    with pytest.raises(EmbeddingError) as caught:
        embedder.embed(["hello"])
    assert str(EMBED_DIM) in caught.value.detail


def test_gateway_embedder_raises_embedding_error_on_malformed_json_body():
    class NonJSONTransport:
        def post(self, url, headers=None, json=None):
            class R:
                status_code = 200

                def json(self):
                    raise ValueError("not json")

            return R()

    embedder = GatewayEmbedder("https://llm.jc.turbo.inc/v1", "key", transport=NonJSONTransport())
    with pytest.raises(EmbeddingError):
        embedder.embed(["hello"])


def test_gateway_embedder_raises_embedding_error_on_missing_embedding_key():
    class MissingKeyTransport:
        def post(self, url, headers=None, json=None):
            class R:
                status_code = 200

                def json(self):
                    return {"data": [{"not_embedding": []}]}

            return R()

    embedder = GatewayEmbedder(
        "https://llm.jc.turbo.inc/v1", "key", transport=MissingKeyTransport()
    )
    with pytest.raises(EmbeddingError):
        embedder.embed(["hello"])


def test_embed_pending_raises_rather_than_misattributing_short_vector_lists(tmp_path):
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    add_chunk(conn, 2)
    conn.commit()

    class ShortEmbedder:
        def embed(self, texts):
            return [[0.0] * EMBED_DIM]  # one vector for two texts

    with pytest.raises(EmbeddingError):
        embed_pending(conn, ShortEmbedder())

    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 1").fetchone()[0] == 0


def test_mid_batch_write_failure_rolls_back_leaving_vector_and_flag_in_sync(tmp_path):
    import sqlite3

    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    add_chunk(conn, 2)
    conn.commit()

    class FlakyConn:
        """Wraps a real connection, failing one specific write mid-batch."""

        def __init__(self, real, fail_on_call):
            self._real = real
            self._n = 0
            self._fail_on_call = fail_on_call

        def execute(self, sql, params=()):
            self._n += 1
            if self._n == self._fail_on_call:
                raise sqlite3.OperationalError("simulated failure")
            return self._real.execute(sql, params)

        def commit(self):
            self._real.commit()

        def rollback(self):
            self._real.rollback()

    # Call sequence: 1 SELECT, then per row DELETE/INSERT/UPDATE.
    # Call 4 is the UPDATE for chunk 1 -- its INSERT (call 3) has already landed.
    flaky = FlakyConn(conn, fail_on_call=4)

    with pytest.raises(sqlite3.OperationalError):
        embed_pending(flaky, FakeEmbedder(), batch=2)

    rows = conn.execute("SELECT id, embedded FROM chunks ORDER BY id").fetchall()
    vector_ids = {r[0] for r in conn.execute("SELECT chunk_id FROM chunks_vec").fetchall()}
    for row in rows:
        assert (row["id"] in vector_ids) == bool(row["embedded"]), (
            f"chunk {row['id']} has a vector/flag mismatch"
        )


def test_a_later_batch_failure_does_not_undo_earlier_committed_batches(tmp_path):
    conn = open_db(tmp_path / "c.db")
    for i in range(8):
        add_chunk(conn, i + 1)
    conn.commit()

    class FailAfterNBatches:
        """Succeeds for the first `ok_batches` calls, then raises EmbeddingError."""

        def __init__(self, ok_batches):
            self._ok_batches = ok_batches
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls > self._ok_batches:
                raise EmbeddingError("simulated gateway failure")
            return [[0.0] * EMBED_DIM for _ in texts]

    with pytest.raises(EmbeddingError):
        embed_pending(conn, FailAfterNBatches(ok_batches=2), batch=2)

    def state(chunk_id):
        embedded = conn.execute(
            "SELECT embedded FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()[0]
        has_vector = (
            conn.execute(
                "SELECT count(*) FROM chunks_vec WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()[0]
            > 0
        )
        return embedded, has_vector

    for chunk_id in (1, 2, 3, 4):  # batches 1-2: committed before the failure
        assert state(chunk_id) == (1, True), f"chunk {chunk_id} should stay committed"

    for chunk_id in (5, 6):  # batch 3: the one that failed
        assert state(chunk_id) == (0, False), f"chunk {chunk_id} should be rolled back"

    for chunk_id in (7, 8):  # batch 4: never reached
        assert state(chunk_id) == (0, False), f"chunk {chunk_id} should be untouched"


def test_fake_embedder_vectors_are_stable_across_process_restarts():
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from sherpa.corpus.embed import FakeEmbedder\n"
        "print(FakeEmbedder().embed(['same text'])[0])\n"
    ) % str(Path(__file__).resolve().parent.parent)

    def run_once(seed):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        return result.stdout

    first = run_once("111")
    second = run_once("222")
    assert first == second


def test_the_fake_embedder_uses_every_dimension():
    """`(hash >> i) % 7` is 0 for every i past the hash's bit length, so dims
    256-767 were identically zero and every distance assertion tuned against
    this fake -- including the ones justifying KNN_DISTANCE_FLOOR -- ran over a
    third of the space."""
    vector = FakeEmbedder().embed(["anything at all"])[0]
    assert len(vector) == 768
    assert any(value for value in vector[256:])
    assert len({value for value in vector[256:]}) > 1


# --- A cold model load is not a fault ---------------------------------------
#
# From a real overnight run: embed_pending died after 126.7s with "The read
# operation timed out", 15,973 chunks pending. Not chunk size and not batch
# size -- once warm a 32-chunk batch embeds in 0.3s. It was ollama pulling
# nomic-embed-text back into VRAM after eviction, which the first request of
# any run on a shared GPU will routinely pay. With no retry, one such stall
# aborts the whole sync, and on a box that evicts between runs that is most
# runs.

class _ColdThenWarm:
    """Times out `stalls` times, then answers normally."""

    def __init__(self, stalls: int, *, dim: int = EMBED_DIM) -> None:
        self.stalls = stalls
        self.dim = dim
        self.attempts = 0

    def post(self, url, headers=None, json=None):
        import httpx

        self.attempts += 1
        if self.attempts <= self.stalls:
            raise httpx.ReadTimeout("The read operation timed out")
        return _Response(200, {"data": [{"embedding": [0.1] * self.dim} for _ in json["input"]]})


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_a_cold_model_load_is_retried_rather_than_fatal(tmp_path):
    conn = open_db(tmp_path / "c.db")
    for i in range(3):
        add_chunk(conn, i + 1)
    conn.commit()

    transport = _ColdThenWarm(stalls=1)
    slept = []
    embedder = GatewayEmbedder(transport=transport, sleep=slept.append)

    assert embed_pending(conn, embedder, batch=2) == 3
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 0
    assert transport.attempts == 3  # one stall, one retry, one more batch
    assert slept  # it backed off rather than hammering


def test_an_unreachable_gateway_is_still_fatal_after_its_retries(tmp_path):
    """Exhausted retries must still abort loudly. No degrading to keyword-only,
    no silently leaving chunks pending -- that distinction is this module."""
    conn = open_db(tmp_path / "c.db")
    add_chunk(conn, 1)
    conn.commit()

    transport = _ColdThenWarm(stalls=99)
    embedder = GatewayEmbedder(transport=transport, sleep=lambda _: None)

    with pytest.raises(EmbeddingError):
        embed_pending(conn, embedder)
    assert transport.attempts == EMBED_RETRIES
    assert conn.execute("SELECT embedded FROM chunks WHERE id = 1").fetchone()[0] == 0


def test_a_bad_request_is_not_retried(tmp_path):
    """A gateway answering 400 will answer 400 again; only transport failures
    and the statuses a loaded-but-busy gateway returns are worth another go."""

    class _Rejecting:
        def __init__(self):
            self.attempts = 0

        def post(self, url, headers=None, json=None):
            self.attempts += 1
            return _Response(400, {})

    transport = _Rejecting()
    embedder = GatewayEmbedder(transport=transport, sleep=lambda _: None)
    with pytest.raises(EmbeddingError):
        embedder.embed(["x"])
    assert transport.attempts == 1


def test_the_embed_phase_stops_at_its_deadline_and_leaves_the_rest_pending(tmp_path):
    """A run that spends its budget on ingest and then embeds for many minutes
    more has no wall-clock bound at all, and a 15-minute cron overlaps itself.
    The remainder stays pending, which `status` already reports."""
    conn = open_db(tmp_path / "c.db")
    for i in range(10):
        add_chunk(conn, i + 1)
    conn.commit()

    ticking = iter(range(1000))
    done = embed_pending(
        conn, FakeEmbedder(), batch=2, now=lambda: float(next(ticking)), deadline=2.0
    )

    assert 0 < done < 10
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 10 - done


def test_the_default_batch_is_the_measured_one(tmp_path):
    """32 real chunks is ~23k characters and embeds in 0.3s warm; 64 doubles
    the payload for no throughput gain and widens the cold-start blast radius."""
    conn = open_db(tmp_path / "c.db")
    for i in range(33):
        add_chunk(conn, i + 1)
    conn.commit()
    embedder = FakeEmbedder()
    embed_pending(conn, embedder)
    assert [len(b) for b in embedder.batches] == [32, 1]
