"""Chunk embeddings via the local LiteLLM gateway.

Failures raise. Nothing here degrades to keyword-only or quietly leaves chunks
pending: a corpus whose semantic half silently stopped working still answers
every question, just worse, and nothing about the answer says so.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Any, Protocol

from sherpa.corpus.db import EMBED_DIM

DEFAULT_MODEL = "ollama/nomic-embed-text:latest"
DEFAULT_BASE_URL = "https://llm.jc.turbo.inc/v1"


class EmbeddingError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class GatewayEmbedder:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        transport: Any = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._transport = transport

    def _client(self):
        if self._transport is None:
            import httpx

            self._transport = httpx.Client(timeout=120.0)
        return self._transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client().post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": self._model, "input": texts},
            )
        except Exception as error:
            raise EmbeddingError(f"embedding gateway unreachable: {error}") from error

        if response.status_code != 200:
            raise EmbeddingError(f"embedding gateway returned HTTP {response.status_code}")

        vectors = [item["embedding"] for item in response.json().get("data", [])]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding gateway returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for vector in vectors:
            if len(vector) != EMBED_DIM:
                raise EmbeddingError(
                    f"embedding gateway returned dimension {len(vector)}, expected {EMBED_DIM}"
                )
        return vectors


class FakeEmbedder:
    """Deterministic vectors for tests; records what it was asked to embed."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self.seen: list[str] = []
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        self.batches.append(list(texts))
        return [[float((hash(t) >> i) % 7) / 7.0 for i in range(self.dim)] for t in texts]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def embed_pending(conn: sqlite3.Connection, embedder: Embedder, *, batch: int = 64) -> int:
    pending = conn.execute(
        "SELECT id, header, text FROM chunks WHERE embedded = 0 ORDER BY id"
    ).fetchall()

    done = 0
    for start in range(0, len(pending), batch):
        window = pending[start : start + batch]
        texts = [f"{row['header']}\n{row['text']}" for row in window]
        vectors = embedder.embed(texts)
        for row, vector in zip(window, vectors):
            conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (row["id"], _pack(vector)),
            )
            conn.execute("UPDATE chunks SET embedded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        done += len(window)

    return done
