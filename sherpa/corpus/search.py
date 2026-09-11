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

# Conservative bound for how many chunk ids we inline as `?` placeholders in a
# single statement. SQLite's actual limit varies by build (999 historically,
# 32766 by default since 3.32.0); this stays well under either, leaving
# headroom for the other bound parameters (the query vector, k) in the same
# statement.
IN_CLAUSE_LIMIT = 900

# sqlite-vec's `MATCH ... k = ?` always returns the k nearest rows present in
# the table, however distant -- an unfiltered KNN literally cannot express "no
# match." Left alone, that means a query about something the corpus has never
# discussed still comes back with confident-looking transcripts, and a
# downstream agent reading them can confabulate a commitment that was never
# made. So a KNN-only hit (one BM25 did not also surface) only counts if its
# distance clears this floor; an exact-token BM25 hit is never subject to it.
# This is an L2 distance against `chunks_vec` (vec0's default metric). Tuned
# for now against untested assumptions about real nomic-embed-text vectors --
# see the Task 9 report for the reasoning and why it may need retuning.
KNN_DISTANCE_FLOOR = 1.2


@dataclass
class Filters:
    person: str | None = None
    mine: bool = False
    workspace: str | None = None
    channel: str | None = None
    since: float | None = None
    until: float | None = None


@dataclass
class Page:
    """A page of hits alongside the true count of matching chunks.

    `conversation_with` and `sent_to` cap what they hydrate to `limit` chunks for
    cost reasons, but the caller must be able to tell "50 of 50 total" from "50 of
    340 total" -- silently returning a truncated list with no total looks
    identical to a complete answer, and a downstream agent reading it has no way
    to know it is missing the other 290.
    """

    hits: list[Hit]
    total: int


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
    end_ts: float


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


def _filters_active(filters: Filters) -> bool:
    """Whether any hard filter narrows the chunk set at all.

    Used to choose between the two KNN paths below, not to guess at how much a
    given filter would narrow the corpus -- the choice is "is there a filter",
    not "is the corpus big".
    """
    return bool(
        filters.person
        or filters.mine
        or filters.workspace
        or filters.channel
        or filters.since is not None
        or filters.until is not None
    )


def _knn_ranked(conn, query: str, embedder: Embedder, filters: Filters) -> list[int]:
    """KNN restricted to the filtered chunk set -- genuinely pre-filtered, never
    a global top-N intersected with the filter afterward.

    A global-KNN-then-intersect approach looks equivalent in a small corpus,
    but is not: at real scale, the true nearest neighbours *within* a filtered
    set (e.g. one person's whole conversation) routinely rank outside the
    global top `k`, so post-filtering finds nothing even when strong matches
    exist inside the filter. `--from hunter` must search hunter's conversation,
    not the global top 50 filtered down to hunter's conversation.

    When no filter narrows anything, there is nothing to pre-filter against,
    so this uses vec0's native `embedding MATCH ? AND k = ?` with no `IN`
    clause -- vec0's own ANN index, which is what it is actually fast at. This
    matters at scale: an unfiltered `sherpa corpus search "..."` is the most
    common interactive query, and the whole corpus (tens of thousands of
    chunks) is the "allowed set" in that case, so a Python-side scan there
    would be a real interactive-latency problem (measured below).

    When a filter is active, this instead fetches the stored embeddings for
    exactly the allowed chunk ids -- by primary-key `IN (...)` lookup with no
    MATCH clause, batched to respect IN_CLAUSE_LIMIT -- and computes L2
    distance against them directly in Python. This was forced by a direct
    empirical finding against this repo's sqlite-vec build (v0.1.9): issuing
    `MATCH ... k = ? AND chunk_id IN (...)` in one statement does not
    genuinely pre-filter -- it computes the global top-`k` neighbours over the
    *entire* table first and only then filters those k rows by the `IN` list,
    so an allowed row ranking outside the global top `k` is dropped even
    though the `IN` clause names it explicitly (reproduced with 100 background
    vectors closer than one `IN`-listed target: dropped at k=50 and k=100,
    only returned once k exceeded the number of closer rows). The
    primary-key-lookup-then-Python-L2 approach sidesteps that entirely: it is
    a true within-allowed-set search by construction, independent of whatever
    vec0's `IN` filtering semantics turn out to be. It is only used when the
    allowed set is filter-narrowed, so it stays small -- the linear scan cost
    that would be unacceptable over the whole corpus is fine over one
    conversation's chunks.

    Rows beyond KNN_DISTANCE_FLOOR are dropped: a chunk BM25 also found still
    enters the fused results through the BM25 ranking regardless of distance,
    but a chunk only vector search surfaces must actually be close.
    """
    vector = embedder.embed([query])[0]

    if not _filters_active(filters):
        packed = struct.pack(f"{len(vector)}f", *vector)
        rows = conn.execute(
            "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [packed, CANDIDATES],
        ).fetchall()
        return [row[0] for row in rows if row[1] <= KNN_DISTANCE_FLOOR][:CANDIDATES]

    clause, params = _where(filters)
    allowed = [row[0] for row in conn.execute(f"SELECT c.id FROM chunks c WHERE {clause}", params).fetchall()]
    if not allowed:
        return []

    rows = _knn_within_allowed(conn, vector, allowed)
    return [row[0] for row in rows if row[1] <= KNN_DISTANCE_FLOOR][:CANDIDATES]


def _knn_within_allowed(conn, vector: list[float], allowed: list[int]) -> list[tuple[int, float]]:
    """Rank exactly the allowed chunks by distance -- a real pre-filtered search.

    Fetches only the allowed embeddings (by primary-key `IN (...)`, batched to
    respect IN_CLAUSE_LIMIT -- a plain lookup, not a MATCH/KNN query, so it is
    an exact filter unaffected by any top-k ranking) and computes L2 distance
    in Python. See _knn_ranked's docstring for why sqlite-vec's own MATCH + IN
    form cannot be used for this instead.
    """
    embeddings: dict[int, bytes] = {}
    for start in range(0, len(allowed), IN_CLAUSE_LIMIT):
        batch = allowed[start : start + IN_CLAUSE_LIMIT]
        placeholders = ",".join("?" * len(batch))
        for chunk_id, blob in conn.execute(
            f"SELECT chunk_id, embedding FROM chunks_vec WHERE chunk_id IN ({placeholders})", batch
        ):
            embeddings[chunk_id] = blob

    dim = len(vector)
    scored = []
    for chunk_id, blob in embeddings.items():
        candidate = struct.unpack(f"{dim}f", blob)
        distance = sum((a - b) ** 2 for a, b in zip(vector, candidate)) ** 0.5
        scored.append((chunk_id, distance))
    scored.sort(key=lambda pair: pair[1])
    return scored[:CANDIDATES]


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
    """True interval overlap, not equal start times.

    Chunks overlap by design (OVERLAP messages carried into the next window),
    and `context` widening extends a hydrated range further into its
    neighbours. Two hits covering the same conversation routinely start at
    different messages, so a bare `start_ts` equality check misses the
    duplicate-transcript case this function exists to catch.
    """
    return (
        a.channel == b.channel
        and a.workspace == b.workspace
        and a.start_ts <= b.end_ts
        and b.start_ts <= a.end_ts
    )


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
        end_ts=messages[-1]["ts"],
    )


def conversation_with(
    conn: sqlite3.Connection, person: str, *, since: float | None = None, limit: int = 50
) -> Page:
    params = [f"%{person}%"]
    clause = "participants LIKE ?"
    if since is not None:
        clause += " AND end_ts >= ?"
        params.append(since)
    total = conn.execute(f"SELECT count(*) FROM chunks WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id FROM chunks WHERE {clause} ORDER BY start_ts DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    hits = [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]
    return Page(hits=hits, total=total)


def sent_to(conn: sqlite3.Connection, person: str, *, limit: int = 50) -> Page:
    """Chunks where Jim spoke, in a conversation involving this person."""
    clause = (
        "c.participants LIKE ?"
        " AND EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
        " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
        " AND m.is_me = 1)"
    )
    total = conn.execute(
        f"SELECT count(*) FROM chunks c WHERE {clause}", (f"%{person}%",)
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.id FROM chunks c WHERE {clause} ORDER BY c.start_ts DESC LIMIT ?",
        (f"%{person}%", limit),
    ).fetchall()
    hits = [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]
    return Page(hits=hits, total=total)


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
