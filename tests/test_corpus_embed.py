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
from sherpa.corpus.embed import EmbeddingError, FakeEmbedder, GatewayEmbedder, embed_pending


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
