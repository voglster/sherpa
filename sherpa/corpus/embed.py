"""Chunk embeddings via the local LiteLLM gateway.

Failures raise. Nothing here degrades to keyword-only or quietly leaves chunks
pending: a corpus whose semantic half silently stopped working still answers
every question, just worse, and nothing about the answer says so.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from typing import Any, Protocol

from sherpa.corpus.db import EMBED_DIM

DEFAULT_MODEL = "ollama/nomic-embed-text:latest"
DEFAULT_BASE_URL = "https://llm.jc.turbo.inc/v1"

# The first request of a run routinely waits for ollama to pull the model back
# into VRAM on a box that also serves other models. Measured cold: 126s. Warm,
# a 32-chunk batch answers in 0.3s -- so a generous ceiling costs nothing in the
# normal case and is the difference between a wasted cron run and a slow one.
DEFAULT_TIMEOUT = 300.0

# A cold load is expected operational behavior, not a fault, so it is retried.
# An unreachable gateway is a fault, so exhausting these still raises.
EMBED_RETRIES = 4
RETRY_BACKOFF_SECONDS = 4.0

# A gateway that is loaded but busy, or briefly gone behind its proxy. A 4xx is
# not here on purpose: a request the gateway rejects will be rejected again.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# 32 real chunks is ~23k characters and embeds in 0.3s warm. 64 doubles the
# payload for no throughput gain and doubles what a cold-start stall costs.
DEFAULT_BATCH = 32

# The embed phase answers to the run's clock like every other phase: a sync
# that spends its budget on ingest and then embeds for minutes more has no
# wall-clock bound, and a 15-minute cron overlaps itself.
EMBED_BUDGET_SHARE = 0.3


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
        sleep=time.sleep,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._transport = transport
        self._sleep = sleep

    def _client(self):
        if self._transport is None:
            import httpx

            self._transport = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._request(texts)

        try:
            body = response.json()
            vectors = [item["embedding"] for item in body.get("data", [])]
        except Exception as error:
            raise EmbeddingError(
                f"embedding gateway returned an unparseable body: {error}"
            ) from error

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

    def _request(self, texts: list[str]):
        """POST the batch, retrying the failures a cold model load produces.

        Retrying transport failures and busy-gateway statuses is the whole
        difference between a slow run and a wasted one. It is not a softening
        of the abort rule: when the attempts run out this still raises
        EmbeddingError, the caller still stops, and nothing degrades to
        keyword-only or quietly leaves chunks pending.
        """
        last = ""
        for attempt in range(EMBED_RETRIES):
            try:
                response = self._client().post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": self._model, "input": texts},
                )
            except Exception as error:
                if not _is_transient(error):
                    raise EmbeddingError(f"embedding gateway unreachable: {error}") from error
                last = f"unreachable: {error}"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in RETRY_STATUS:
                    raise EmbeddingError(
                        f"embedding gateway returned HTTP {response.status_code}"
                    )
                last = f"HTTP {response.status_code}"

            if attempt + 1 < EMBED_RETRIES:
                self._sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise EmbeddingError(
            f"embedding gateway unreachable after {EMBED_RETRIES} attempts ({last})"
        )


def _is_transient(error: Exception) -> bool:
    """Whether another attempt could plausibly succeed.

    A timeout or a refused connection is the gateway being slow or briefly
    away; anything else is this code asking for something wrong.
    """
    try:
        import httpx

        return isinstance(error, httpx.TransportError)
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        return isinstance(error, (TimeoutError, ConnectionError, OSError))


def _stable_hash(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")


class FakeEmbedder:
    """Deterministic vectors for tests; records what it was asked to embed.

    Uses a SHA-256-derived hash rather than Python's ``hash()``, which is
    salted per process -- a vector compared across process invocations (as
    Task 9's fixtures may do) needs to be reproducible, not merely stable
    within one run.
    """

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self.seen: list[str] = []
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        self.batches.append(list(texts))
        # Per-dimension hashes, not shifts of one: a SHA-256 digest is 256 bits,
        # so `hash >> i` is 0 for every i past that and the fake would be
        # identically zero over two thirds of the space it claims to fill.
        return [
            [float(_stable_hash(f"{t}#{i}") % 7) / 7.0 for i in range(self.dim)]
            for t in texts
        ]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def embed_pending(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    batch: int = DEFAULT_BATCH,
    deadline: float | None = None,
    now=time.time,
) -> int:
    pending = conn.execute(
        "SELECT id, header, text FROM chunks WHERE embedded = 0 ORDER BY id"
    ).fetchall()

    done = 0
    for start in range(0, len(pending), batch):
        # Between batches, never inside one: a batch is already committed as a
        # unit, and what is left stays pending, which `status` reports.
        if deadline is not None and start and now() >= deadline:
            break
        window = pending[start : start + batch]
        texts = [f"{row['header']}\n{row['text']}" for row in window]
        vectors = embedder.embed(texts)
        if len(vectors) != len(window):
            raise EmbeddingError(
                f"embedder returned {len(vectors)} vectors for {len(window)} inputs"
            )

        try:
            for row, vector in zip(window, vectors):
                conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (row["id"],))
                conn.execute(
                    "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (row["id"], _pack(vector)),
                )
                conn.execute("UPDATE chunks SET embedded = 1 WHERE id = ?", (row["id"],))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        done += len(window)

    return done
