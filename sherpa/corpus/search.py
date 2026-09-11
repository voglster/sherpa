"""Hybrid retrieval: BM25 and vector KNN behind hard filters, fused with RRF.

Filters are applied BEFORE fusion, not after, so a `--from hunter` query spends
its whole k-budget inside that conversation rather than retrieving globally and
discarding most of it.

RRF is used rather than weighted score blending because BM25 scores and cosine
distances are not on comparable scales; RRF is rank-only and sidesteps that.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from dataclasses import dataclass

from sherpa.corpus.chunk import format_script
from sherpa.corpus.embed import Embedder

RRF_K = 60
CANDIDATES = 50


@dataclass
class Filters:
    person: str | None = None
    mine: bool = False
    workspace: str | None = None
    channel: str | None = None
    since: float | None = None
    until: float | None = None


@dataclass
class Hit:
    chunk_id: int
    score: float
    header: str
    transcript: str
    permalink: str
    channel: str
    workspace: str
    start_ts: float


def _where(filters: Filters) -> tuple[str, list]:
    clauses, params = ["1 = 1"], []
    if filters.workspace:
        clauses.append("c.workspace = ?")
        params.append(filters.workspace)
    if filters.channel:
        clauses.append("c.channel_id = ?")
        params.append(filters.channel)
    if filters.since is not None:
        clauses.append("c.end_ts >= ?")
        params.append(filters.since)
    if filters.until is not None:
        clauses.append("c.start_ts <= ?")
        params.append(filters.until)
    if filters.person:
        clauses.append("c.participants LIKE ?")
        params.append(f"%{filters.person}%")
    if filters.mine:
        clauses.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
            " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
            " AND m.is_me = 1)"
        )
    return " AND ".join(clauses), params


def _bm25_ranked(conn, query: str, filters: Filters) -> list[int]:
    where, params = _where(filters)
    sanitized = _fts_query(query)
    if not sanitized:
        return []
    rows = conn.execute(
        f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.chunk_id"
        f" WHERE chunks_fts MATCH ? AND {where}"
        f" ORDER BY bm25(chunks_fts) LIMIT ?",
        [sanitized, *params, CANDIDATES],
    ).fetchall()
    return [row[0] for row in rows]


def _fts_query(query: str) -> str:
    """FTS5 treats punctuation as syntax; quote each term so KB-51508 is literal."""
    terms = [t for t in re.findall(r"[\w\-\.]+", query) if t]
    return " OR ".join(f'"{term}"' for term in terms)


def _knn_ranked(conn, query: str, embedder: Embedder, filters: Filters) -> list[int]:
    """KNN restricted to the filtered chunk set.

    The brief's single-statement form (`embedding MATCH ? AND k = ? AND chunk_id
    IN (...)`) errors under sqlite-vec: a KNN MATCH cannot be combined with an
    auxiliary `IN` predicate in the same query (sqlite3.OperationalError). So the
    KNN runs unfiltered with a widened k, and the filtered id set is intersected
    in Python. This also sidesteps the brief's noted ~10k-placeholder SQLite
    variable limit on the inlined id list, since that list never becomes SQL
    parameters here.
    """
    clause, params = _where(filters)
    allowed = {row[0] for row in conn.execute(f"SELECT c.id FROM chunks c WHERE {clause}", params).fetchall()}
    if not allowed:
        return []

    vector = embedder.embed([query])[0]
    widened_k = max(CANDIDATES, len(allowed))
    rows = conn.execute(
        "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        [struct.pack(f"{len(vector)}f", *vector), widened_k],
    ).fetchall()
    return [row[0] for row in rows if row[0] in allowed][:CANDIDATES]


def _fuse(*rankings: list[int]) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder,
    *,
    filters: Filters | None = None,
    k: int = 5,
    context: int = 3,
) -> list[Hit]:
    filters = filters or Filters()
    fused = _fuse(
        _bm25_ranked(conn, query, filters),
        _knn_ranked(conn, query, embedder, filters),
    )

    hits, covered = [], []
    for chunk_id, score in fused:
        hit = _hydrate(conn, chunk_id, score, context)
        if hit is None:
            continue
        if any(_overlaps(hit, seen) for seen in covered):
            continue
        covered.append(hit)
        hits.append(hit)
        if len(hits) >= k:
            break
    return hits


def _overlaps(a: Hit, b: Hit) -> bool:
    return a.channel == b.channel and a.workspace == b.workspace and a.start_ts == b.start_ts


def _hydrate(conn, chunk_id: int, score: float, context: int) -> Hit | None:
    chunk = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    if chunk is None:
        return None

    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ?"
        " AND ts BETWEEN ? AND ? ORDER BY ts",
        (chunk["workspace"], chunk["channel_id"], chunk["start_ts"], chunk["end_ts"]),
    ).fetchall()

    if context:
        before = conn.execute(
            "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND ts < ?"
            " ORDER BY ts DESC LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], chunk["start_ts"], context),
        ).fetchall()
        after = conn.execute(
            "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND ts > ?"
            " ORDER BY ts LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], chunk["end_ts"], context),
        ).fetchall()
        messages = list(reversed(before)) + list(messages) + list(after)

    if not messages:
        return None

    return Hit(
        chunk_id=chunk_id,
        score=score,
        header=chunk["header"],
        transcript=format_script(messages),
        permalink=messages[0]["permalink"],
        channel=chunk["channel_id"],
        workspace=chunk["workspace"],
        start_ts=messages[0]["ts"],
    )


def conversation_with(
    conn: sqlite3.Connection, person: str, *, since: float | None = None, limit: int = 50
) -> list[Hit]:
    params = [f"%{person}%"]
    clause = "participants LIKE ?"
    if since is not None:
        clause += " AND end_ts >= ?"
        params.append(since)
    rows = conn.execute(
        f"SELECT id FROM chunks WHERE {clause} ORDER BY start_ts DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]


def sent_to(conn: sqlite3.Connection, person: str, *, limit: int = 50) -> list[Hit]:
    """Chunks where Jim spoke, in a conversation involving this person."""
    rows = conn.execute(
        "SELECT c.id FROM chunks c WHERE c.participants LIKE ?"
        " AND EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
        " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
        " AND m.is_me = 1)"
        " ORDER BY c.start_ts DESC LIMIT ?",
        (f"%{person}%", limit),
    ).fetchall()
    return [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]


def thread_at(conn: sqlite3.Connection, permalink_or_id: str) -> Hit | None:
    message = conn.execute(
        "SELECT * FROM messages WHERE permalink = ? OR id = ?",
        (permalink_or_id, permalink_or_id),
    ).fetchone()
    if message is None:
        return None

    chunk = conn.execute(
        "SELECT id FROM chunks WHERE workspace = ? AND channel_id = ?"
        " AND ? BETWEEN start_ts AND end_ts ORDER BY start_ts LIMIT 1",
        (message["workspace"], message["channel_id"], message["ts"]),
    ).fetchone()
    return _hydrate(conn, chunk[0], 0.0, 3) if chunk else None
