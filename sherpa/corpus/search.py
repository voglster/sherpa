"""Hybrid retrieval: BM25 and vector KNN behind hard filters, fused with RRF.

Filters are applied BEFORE fusion, not after, so a `--from hunter` query spends
its whole k-budget inside that conversation rather than retrieving globally and
discarding most of it.

RRF is used rather than weighted score blending because BM25 scores and cosine
distances are not on comparable scales; RRF is rank-only and sidesteps that.
"""

from __future__ import annotations

import random
import re
import sqlite3
import statistics
import struct
import time
from dataclasses import dataclass

from sherpa.corpus.chunk import format_script
from sherpa.corpus.embed import Embedder

RRF_K = 60
CANDIDATES = 50

# A mild preference for recent conversation, applied to the fused score.
#
# The corpus spans five years, and "what did I promise" or "did I miss
# anything" is almost never answered by a 2021 thread -- but sometimes it is,
# so this is a tie-break, not a cutoff (`--since` is the cutoff, and it already
# exists). Sizing: adjacent RRF ranks differ by about 1.5%, while a chunk found
# by both BM25 and KNN scores roughly double a chunk found by one. A 5% ceiling
# therefore reorders neighbours -- a recent chunk can pass an older one two or
# three ranks above it -- and cannot come close to overturning a genuinely
# stronger match. The half-life is deliberately long for the same reason: work
# conversations stay live for months.
RECENCY_WEIGHT = 0.05
RECENCY_HALF_LIFE_DAYS = 180.0

# A person is in a conversation whether or not they spoke in the window this
# chunk covers. `people` is the conversation's roster plus everyone the window
# names; `participants` is only who spoke, and is still matched so that chunks
# written before `people` existed keep answering until they are rebuilt.
PERSON_CLAUSE = "({prefix}people LIKE ? OR {prefix}participants LIKE ?)"


# A thread and the channel around it overlap in time, so "Jim spoke inside this
# chunk's ts range" catches him talking in the channel while someone else's
# thread ran -- and `owed` would hand that thread back as something he said.
SAME_CONVERSATION = (
    "((c.thread_ts IS NULL AND m.thread_ts IS NULL) OR m.thread_ts = c.thread_ts)"
)


def person_params(person: str) -> list[str]:
    return [f"%{person}%", f"%{person}%"]

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
# made. So a KNN-only hit (one BM25 did not also surface) has to clear a bar;
# an exact-token BM25 hit is never subject to it.
#
# THE BAR IS RELATIVE, because an absolute one cannot hold. The more chunks
# there are, the closer the nearest neighbour of ANY query is, including a
# meaningless one: 0.96 rejected nonsense at 44k chunks and admitted it at 72k,
# the same way 1.2 admitted everything before that. Re-tuning the constant as
# the corpus grows is a treadmill that ends with the guard doing nothing.
#
# So each query is measured against its own background: sample a few hundred
# random chunks, take the mean and spread of their distances from the query
# vector, and accept a vector-only hit only if it is BACKGROUND_SIGMA standard
# deviations below that mean. The question becomes "does this stand out from
# noise for this query", which is scale-invariant by construction -- as the
# corpus grows the background mean moves down and the bar moves with it.
#
# CALIBRATION. L2 distances against `chunks_vec` for `nomic-embed-text`
# (768-dim), measured over a live 625k-message corpus, 600 random chunks
# sampled per query:
#
#   background over random chunks    mean ~1.12, p1 ~0.96-1.05, p5 ~0.99-1.09
#   nearest true match, real queries      0.867 - 0.948   (~3.4-5.1 sigma below)
#   nearest match, nonsense queries       0.982 - 1.013   (~2.1-2.8 sigma below)
#
# 3.0 sigma sits inside that separation. The margin is real but not wide, and
# it rests on the same eight queries the absolute number did -- what changed is
# that the rule no longer decays as the corpus grows. If real searches start
# coming back empty, lower BACKGROUND_SIGMA first.
BACKGROUND_SIGMA = 3.0

# Enough samples for a stable mean and spread, few enough to stay cheap: the
# cost is 200 L2 distances in Python, about 25ms, and flat in corpus size.
# Deleted ids simply drop out of the sample; MIN_BACKGROUND_SAMPLE covers the
# case where too many do.
BACKGROUND_SAMPLE = 200

# Below this many usable samples the distribution means nothing, and on a
# corpus that small a linear scan of it is the whole corpus anyway. The
# absolute floor takes over -- it is the fallback, not the decider.
MIN_BACKGROUND_SAMPLE = 30

# Fallback bar, and the value `knn_distance_floor` in corpus.toml overrides.
# Only consulted when the background sample is unusable (a new or tiny corpus,
# or vectors so uniform they have no spread).
KNN_DISTANCE_FLOOR = 0.96

# A BM25 hit counts only if the chunk carries at least this many of the query's
# substantive terms -- or all of them, when the query has fewer. One shared word
# is what makes an OR query match the whole corpus; requiring two is the cheapest
# guard that still lets a single deliberate term ("KB-51508") answer on its own.
MIN_MATCHING_TERMS = 2

# Function words carry no discrimination in a corpus of English chat, and FTS5
# has no list of its own. Deliberately short: anything domain-specific belongs
# in the index, not in here.
STOPWORDS = frozenset("""
a about all am an and any are as at be been being but by can cant could did do
does doing dont for from get got had has have he her here hers him his how i id
if ill im in into is it its ive just like ll me might must my no nor not of off
on once one only or other our out over own re said same she should so some such
than that the their them then there these they this those to too up us ve very
was we were what when where which while who whom why will with would you your
""".split())


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
        clauses.append(PERSON_CLAUSE.replace("{prefix}", "c."))
        params.extend(person_params(filters.person))
    if filters.mine:
        clauses.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
            " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
            f" AND {SAME_CONVERSATION} AND m.is_me = 1)"
        )
    return " AND ".join(clauses), params


def _bm25_ranked(conn, query: str, filters: Filters) -> list[int]:
    where, params = _where(filters)
    terms = _substantive_terms(query)
    if not terms:
        return []
    required = min(MIN_MATCHING_TERMS, len(terms))
    rows = conn.execute(
        f"SELECT c.id, c.header || ' ' || c.text || ' ' || c.participants AS body"
        f" FROM chunks_fts f JOIN chunks c ON c.id = f.chunk_id"
        f" WHERE chunks_fts MATCH ? AND {where}"
        f" ORDER BY bm25(chunks_fts) LIMIT ?",
        [_fts_query(terms), *params, CANDIDATES],
    ).fetchall()
    return [
        row["id"]
        for row in rows
        if sum(term.lower() in row["body"].lower() for term in terms) >= required
    ]


@dataclass
class Coverage:
    """How many chunks mention each of the query's substantive terms.

    The adaptive floor answers "is anything semantically close to this". This
    answers a different and more useful question: "does the corpus contain any
    record of this thing at all". A well-formed question about a topic that was
    never discussed -- "the acquisition of Northwind Logistics" -- sails
    through any distance rule, because well-formed business English is close to
    business Slack in embedding space whether or not the topic exists.
    Embeddings measure topical similarity, not factual presence. Term counts
    measure presence directly, and they are a fact about the corpus rather than
    a guess about geometry.
    """

    counts: dict[str, int]

    @property
    def missing(self) -> list[str]:
        return [term for term, count in self.counts.items() if not count]

    @property
    def nothing_present(self) -> bool:
        """Every substantive term is absent, so there is nothing to find."""
        return bool(self.counts) and not any(self.counts.values())


def term_coverage(conn: sqlite3.Connection, query: str) -> Coverage:
    """Count the chunks mentioning each substantive term of the query.

    Uses `_substantive_terms` and the same quoting `_fts_query` uses, so this
    cannot disagree with what BM25 actually searched for. FTS5's tokenizer
    folds case, so the counts are case-insensitive by construction.
    """
    counts = {}
    for term in _substantive_terms(query):
        counts[term] = conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            (_fts_query([term]),),
        ).fetchone()[0]
    return Coverage(counts=counts)


def _substantive_terms(query: str) -> list[str]:
    """The words in a query that can actually discriminate between chunks.

    FTS5 ships no stopword list, so ORing every token means "what did I say
    about the thing" matches any chunk containing "the" -- which is all of
    them. BM25 then hands fusion 50 candidates for any query whatsoever, and
    the KNN distance floor guarding the other half of the pipeline is moot,
    because every confabulated hit arrives through this half instead.
    """
    seen, terms = set(), []
    for token in re.findall(r"[\w\-\.]+", query):
        lowered = token.lower()
        if lowered in STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
    return terms


def _fts_query(terms: list[str]) -> str:
    """FTS5 treats punctuation as syntax; quote each term so KB-51508 is literal."""
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


def _knn_ranked(
    conn, query: str, embedder: Embedder, filters: Filters, floor: float = KNN_DISTANCE_FLOOR
) -> list[int]:
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
    bar = _relevance_bar(conn, vector, floor)

    if not _filters_active(filters):
        packed = struct.pack(f"{len(vector)}f", *vector)
        rows = conn.execute(
            "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [packed, CANDIDATES],
        ).fetchall()
        return [row[0] for row in rows if row[1] <= bar][:CANDIDATES]

    clause, params = _where(filters)
    allowed = [row[0] for row in conn.execute(f"SELECT c.id FROM chunks c WHERE {clause}", params).fetchall()]
    if not allowed:
        return []

    rows = _knn_within_allowed(conn, vector, allowed)
    return [row[0] for row in rows if row[1] <= bar][:CANDIDATES]


def _relevance_bar(conn, vector: list[float], floor: float) -> float:
    """How close a vector hit must be to count, for THIS query.

    Sampled rather than assumed: the distances from this query to a few hundred
    random chunks describe what an unrelated chunk looks like for it, and a
    real match has to stand out from that. An absolute threshold answers a
    different question -- "is this number small" -- whose meaning drifts as the
    corpus grows and the nearest neighbour of everything gets closer.
    """
    # Random ids rather than `ORDER BY random()`, which scans the whole table
    # and would put a cost that grows with the corpus on every query -- the
    # same treadmill in a different place. Probing ids is index lookups, flat
    # in corpus size. Ids that no longer exist simply drop out, which is what
    # the oversampling covers.
    top = conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0
    if top < MIN_BACKGROUND_SAMPLE:
        return floor
    sample = random.sample(range(1, top + 1), min(BACKGROUND_SAMPLE, top))
    distances = [distance for _, distance in _distances_to(conn, vector, sample)]
    if len(distances) < MIN_BACKGROUND_SAMPLE:
        return floor

    spread = statistics.pstdev(distances)
    if spread <= 0:
        return floor
    return statistics.fmean(distances) - BACKGROUND_SIGMA * spread


def _distances_to(conn, vector: list[float], chunk_ids: list[int]) -> list[tuple[int, float]]:
    """L2 distance from `vector` to each stored embedding, by primary key."""
    embeddings: dict[int, bytes] = {}
    for start in range(0, len(chunk_ids), IN_CLAUSE_LIMIT):
        batch = chunk_ids[start : start + IN_CLAUSE_LIMIT]
        placeholders = ",".join("?" * len(batch))
        for chunk_id, blob in conn.execute(
            f"SELECT chunk_id, embedding FROM chunks_vec WHERE chunk_id IN ({placeholders})", batch
        ):
            embeddings[chunk_id] = blob

    dim = len(vector)
    scored = []
    for chunk_id, blob in embeddings.items():
        candidate = struct.unpack(f"{dim}f", blob)
        scored.append(
            (chunk_id, sum((a - b) ** 2 for a, b in zip(vector, candidate)) ** 0.5)
        )
    return scored


def _knn_within_allowed(conn, vector: list[float], allowed: list[int]) -> list[tuple[int, float]]:
    """Rank exactly the allowed chunks by distance -- a real pre-filtered search.

    Fetches only the allowed embeddings (by primary-key `IN (...)`, batched to
    respect IN_CLAUSE_LIMIT -- a plain lookup, not a MATCH/KNN query, so it is
    an exact filter unaffected by any top-k ranking) and computes L2 distance
    in Python. See _knn_ranked's docstring for why sqlite-vec's own MATCH + IN
    form cannot be used for this instead.
    """
    scored = _distances_to(conn, vector, allowed)
    scored.sort(key=lambda pair: pair[1])
    return scored[:CANDIDATES]


def _fuse(*rankings: list[int]) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _recency_factor(end_ts: float, current: float) -> float:
    age_days = max(current - end_ts, 0.0) / 86400.0
    return 1.0 + RECENCY_WEIGHT * 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def _prefer_recent(conn, fused: list[tuple[int, float]], current: float) -> list[tuple[int, float]]:
    """Re-rank the fused candidates, nudging recent conversation upward.

    Applied after RRF rather than inside it, so it adjusts an ordering that
    relevance has already decided instead of competing with it.
    """
    if not fused:
        return fused
    ids = [chunk_id for chunk_id, _ in fused]
    ends = dict(
        conn.execute(
            "SELECT id, end_ts FROM chunks WHERE id IN ({})".format(",".join("?" * len(ids))),
            ids,
        )
    )
    adjusted = [
        (chunk_id, score * _recency_factor(ends.get(chunk_id, current), current))
        for chunk_id, score in fused
    ]
    adjusted.sort(key=lambda pair: pair[1], reverse=True)
    return adjusted


def search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder,
    *,
    filters: Filters | None = None,
    k: int = 5,
    context: int = 3,
    now=time.time,
    knn_distance_floor: float = KNN_DISTANCE_FLOOR,
    coverage: Coverage | None = None,
) -> list[Hit]:
    filters = filters or Filters()
    # No term in the query appears anywhere in the corpus. Whatever the vector
    # index would offer is a topical neighbour of a subject nobody has ever
    # discussed, and handing that back is the confabulation this whole module
    # guards against.
    if (coverage or term_coverage(conn, query)).nothing_present:
        return []
    fused = _prefer_recent(
        conn,
        _fuse(
            _bm25_ranked(conn, query, filters),
            _knn_ranked(conn, query, embedder, filters, knn_distance_floor),
        ),
        now(),
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


def _conversation_scope(chunk) -> tuple[str, list]:
    if chunk["thread_ts"] is None:
        return "thread_ts IS NULL", []
    return "thread_ts = ?", [chunk["thread_ts"]]


def _hydrate(conn, chunk_id: int, score: float, context: int) -> Hit | None:
    chunk = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    if chunk is None:
        return None

    # A thread and the channel around it run at the same time, so a range on ts
    # alone merges two conversations into one transcript -- and the transcript
    # is what the agent reads and summarizes. A thread chunk hydrates to its
    # thread; a loose chunk stays out of the threads it deliberately excluded.
    scope, scope_params = _conversation_scope(chunk)

    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ?"
        f" AND {scope} AND ts BETWEEN ? AND ? ORDER BY ts",
        (chunk["workspace"], chunk["channel_id"], *scope_params,
         chunk["start_ts"], chunk["end_ts"]),
    ).fetchall()

    if context:
        before = conn.execute(
            f"SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND {scope}"
            " AND ts < ? ORDER BY ts DESC LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], *scope_params,
             chunk["start_ts"], context),
        ).fetchall()
        after = conn.execute(
            f"SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND {scope}"
            " AND ts > ? ORDER BY ts LIMIT ?",
            (chunk["workspace"], chunk["channel_id"], *scope_params,
             chunk["end_ts"], context),
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
    params = person_params(person)
    clause = PERSON_CLAUSE.replace("{prefix}", "")
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
        PERSON_CLAUSE.replace("{prefix}", "c.")
        + " AND EXISTS (SELECT 1 FROM messages m WHERE m.workspace = c.workspace"
        " AND m.channel_id = c.channel_id AND m.ts BETWEEN c.start_ts AND c.end_ts"
        f" AND {SAME_CONVERSATION} AND m.is_me = 1)"
    )
    total = conn.execute(
        f"SELECT count(*) FROM chunks c WHERE {clause}", person_params(person)
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.id FROM chunks c WHERE {clause} ORDER BY c.start_ts DESC LIMIT ?",
        [*person_params(person), limit],
    ).fetchall()
    hits = [h for h in (_hydrate(conn, row[0], 0.0, 0) for row in rows) if h]
    return Page(hits=hits, total=total)


def thread_at(conn: sqlite3.Connection, permalink_or_id: str) -> Hit | None:
    """The whole thread around a message, or its conversation window if loose.

    A thread can span several chunks and a chunk can start mid-thread, so
    answering from the chunk that happens to cover the message returns a
    time-slice of it -- and, before the hydration scope was fixed, a slice of
    the channel instead.
    """
    message = conn.execute(
        "SELECT * FROM messages WHERE permalink = ? OR id = ?",
        (permalink_or_id, permalink_or_id),
    ).fetchone()
    if message is None:
        return None

    if message["thread_ts"] is not None:
        return _thread_hit(conn, message)

    chunk = conn.execute(
        "SELECT id FROM chunks WHERE workspace = ? AND channel_id = ?"
        " AND ? BETWEEN start_ts AND end_ts ORDER BY start_ts LIMIT 1",
        (message["workspace"], message["channel_id"], message["ts"]),
    ).fetchone()
    return _hydrate(conn, chunk[0], 0.0, 3) if chunk else None


def _thread_hit(conn: sqlite3.Connection, message) -> Hit | None:
    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? AND thread_ts = ?"
        " ORDER BY ts",
        (message["workspace"], message["channel_id"], message["thread_ts"]),
    ).fetchall()
    if not messages:
        return None

    header = conn.execute(
        "SELECT header FROM chunks WHERE workspace = ? AND channel_id = ? AND thread_ts = ?"
        " ORDER BY start_ts LIMIT 1",
        (message["workspace"], message["channel_id"], message["thread_ts"]),
    ).fetchone()

    return Hit(
        chunk_id=0,
        score=0.0,
        header=header["header"] if header else messages[0]["channel_name"],
        transcript=format_script(messages),
        permalink=messages[0]["permalink"],
        channel=message["channel_id"],
        workspace=message["workspace"],
        start_ts=messages[0]["ts"],
        end_ts=messages[-1]["ts"],
    )
