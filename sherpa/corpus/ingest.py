"""Message ingest: tiered backfill, per-channel cursors, thread sweep.

Slack's channel cursor does not advance when a reply lands on an old thread
parent, so replies need their own sweep or they are lost forever — the single
most likely way for this corpus to go quietly incomplete.

History arrives newest-first, so coverage is tracked as a contiguous window with
a cursor at each end: `sync_state.last_ts` on top, `backfill_state.oldest_ts`
underneath. Catch-up raises the top only when its pass finished, and backfill
lowers the bottom one committed page at a time, so an interrupted run leaves
gaps nowhere and still makes progress everywhere.

The history pagination, the thread sweep, and the tier scheduler all respect
the same wall-clock budget, and none of them may let one channel or one tier
monopolize it: a channel with years of backlog or hundreds of live threads
must degrade to slower, incremental progress rather than starve the rest of
the workspace out of ever backfilling at all.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from sherpa.corpus.directory import Resolver, sync_directory, tier_for
from sherpa.corpus.slack import AuthError, SlackClient, SlackError

SKIP_SUBTYPES = {"tombstone", "channel_join", "channel_leave", "message_deleted"}

# Slack's own per-page maximum for conversations.history.
HISTORY_PAGE_LIMIT = 200

# Bounded so a large corpus does not re-fetch every thread it has ever seen on every run.
THREAD_SWEEP_WINDOW_SECONDS = 30 * 24 * 60 * 60

# Bounded so a channel with a large backlog of live threads sweeps a little more
# of it every run instead of trying to re-verify all of it in one run and, in a
# busy workspace, never finishing before the budget runs out.
THREAD_SWEEP_PER_CHANNEL_CAP = 25

# DMs get the largest guaranteed share since that's where the highest-value
# content lives, but every tier gets *some* guaranteed time regardless of how
# much backlog a higher tier has — a single busy tier-1 channel must not be
# able to consume the whole run and leave lower tiers permanently unbackfilled.
TIER_BUDGET_SHARE = {1: 0.5, 2: 0.3, 3: 0.2}

# A channel with a long backlog always exhausts the history budget, so a sweep
# paid out of what history leaves behind is paid nothing — for the whole length
# of a backfill, which on a real workspace is days. It gets its own reserved
# slice of each channel's time instead.
THREAD_SWEEP_BUDGET_SHARE = 0.3


@dataclass
class IngestReport:
    workspace: str
    messages: int = 0
    channels_done: int = 0
    channels_failed: list[tuple[str, str]] = field(default_factory=list)
    threads_failed: list[tuple[str, str, str]] = field(default_factory=list)


def message_id(workspace: str, channel_id: str, ts: str) -> str:
    return f"{workspace}:{channel_id}:{ts}"


def ingest_workspace(
    conn: sqlite3.Connection,
    client: SlackClient,
    workspace: str,
    me: str | None,
    *,
    budget_seconds: float = 900.0,
    now=time.time,
) -> IngestReport:
    sync_directory(conn, client, workspace, me)
    resolver = Resolver(conn, workspace)
    report = IngestReport(workspace=workspace)
    overall_deadline = now() + budget_seconds

    for tier, channels in _channels_by_tier(conn, workspace).items():
        share = TIER_BUDGET_SHARE.get(tier, 0.0)
        tier_deadline = min(overall_deadline, now() + budget_seconds * share)

        for channel in channels:
            if now() >= tier_deadline:
                break
            try:
                report.messages += _ingest_channel(
                    conn, client, resolver, workspace, channel, now, tier_deadline, report
                )
                report.channels_done += 1
                _mark(conn, workspace, channel["id"], "ok", "")
            except AuthError:
                raise
            except SlackError as error:
                report.channels_failed.append((channel["id"], error.code))
                _mark(conn, workspace, channel["id"], "failed", error.code)

    conn.commit()
    return report


def _channels_by_tier(conn: sqlite3.Connection, workspace: str) -> dict[int, list[sqlite3.Row]]:
    posted_in = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE workspace = ? AND is_me = 1",
            (workspace,),
        )
    }
    channels = conn.execute(
        "SELECT * FROM channels WHERE workspace = ? AND is_member = 1", (workspace,)
    ).fetchall()

    grouped: dict[int, list[sqlite3.Row]] = {}
    for channel in channels:
        tier = tier_for(channel, channel["id"] in posted_in)
        conn.execute(
            "INSERT INTO sync_state (workspace, channel_id, tier) VALUES (?, ?, ?)"
            " ON CONFLICT (workspace, channel_id) DO UPDATE SET tier = excluded.tier",
            (workspace, channel["id"], tier),
        )
        grouped.setdefault(tier, []).append(channel)
    conn.commit()

    return dict(sorted(grouped.items()))


def _ingest_channel(conn, client, resolver, workspace: str, channel, now, deadline, report) -> int:
    """Cover a channel from both ends of its contiguous window.

    `sync_state.last_ts` is the top of that window and `backfill_state.oldest_ts`
    the bottom. Forward catch-up raises the top, backward backfill lowers the
    bottom, and everything between them is known to be stored.

    The two ends move independently. A finished backfill says only that there is
    nothing older left to fetch; catch-up keeps running for the life of the
    channel, including for a channel that was empty when first seen and has a
    bottom cursor of nothing at all.
    """
    channel_id = channel["id"]
    visited, covered_from, backfill_complete = _backfill_state(conn, workspace, channel_id)
    thread_parents: set[str] = set()
    stored = 0

    # History gets everything but the sweep's reserved slice. A channel deep in
    # backfill would otherwise consume the whole budget every run and its
    # threads would never be read at all.
    started = now()
    history_deadline = started + max(deadline - started, 0.0) * (1 - THREAD_SWEEP_BUDGET_SHARE)

    if visited:
        stored += _catch_up(
            conn, client, resolver, workspace, channel, thread_parents, now, history_deadline
        )
    if not backfill_complete:
        stored += _backfill(
            conn, client, resolver, workspace, channel, covered_from, thread_parents,
            now, history_deadline,
        )
    stored += _sweep_threads(
        conn, client, resolver, workspace, channel, thread_parents, now, deadline, report
    )
    conn.commit()
    return stored


def _catch_up(conn, client, resolver, workspace: str, channel, thread_parents, now, deadline) -> int:
    """Fetch everything newer than the cursor, advancing it only on a clean pass.

    `conversations.history` delivers newest-first, so a pass cut short by the
    deadline has stored the newest slice of what it asked for, not the oldest.
    Advancing the cursor there would strand every older message of the same page
    below it forever, so an interrupted pass leaves the cursor where it was and
    the next run asks the same question again.
    """
    channel_id = channel["id"]
    cursor = conn.execute(
        "SELECT last_ts, last_ts_raw FROM sync_state WHERE workspace = ? AND channel_id = ?",
        (workspace, channel_id),
    ).fetchone()
    last_ts = cursor["last_ts"]
    last_ts_raw = cursor["last_ts_raw"] or (f"{last_ts}" if last_ts else "")

    stored = 0
    newest, newest_raw = last_ts, last_ts_raw
    oldest_seen, oldest_seen_raw = None, ""
    completed = True

    for raw in client.paginate(
        "conversations.history",
        "messages",
        channel=channel_id,
        oldest=last_ts_raw or None,
        inclusive=False,
    ):
        if now() >= deadline:
            completed = False
            break
        ts = float(raw["ts"])
        if ts > newest:
            newest, newest_raw = ts, raw["ts"]
        if oldest_seen is None or ts < oldest_seen:
            oldest_seen, oldest_seen_raw = ts, raw["ts"]
        _note_thread_parent(conn, workspace, channel_id, raw, thread_parents)
        stored += _store(conn, resolver, workspace, channel, raw)

    if completed:
        conn.execute(
            "UPDATE sync_state SET last_ts = ?, last_ts_raw = ?, updated_at = ?"
            " WHERE workspace = ? AND channel_id = ?",
            (newest, newest_raw, time.time(), workspace, channel_id),
        )
        if not last_ts and oldest_seen is not None:
            # This pass carried no lower bound, so it read the channel to its
            # start: the floor is whatever it found there.
            conn.execute(
                "UPDATE backfill_state SET oldest_ts = COALESCE(oldest_ts, ?),"
                " oldest_ts_raw = CASE WHEN oldest_ts IS NULL THEN ? ELSE oldest_ts_raw END,"
                " updated_at = ?"
                " WHERE workspace = ? AND channel_id = ?",
                (oldest_seen, oldest_seen_raw, time.time(), workspace, channel_id),
            )
    conn.commit()
    return stored


def _backfill(
    conn, client, resolver, workspace: str, channel, covered_from, thread_parents, now, deadline
) -> int:
    """Page backwards through history, committing each completed page.

    Paging by `latest` rather than by Slack's cursor token makes the position
    durable: a run interrupted after three pages resumes at page four instead of
    restarting from the newest page, so a first backfill of a long history
    finishes across however many runs it takes.
    """
    channel_id = channel["id"]
    floor, floor_raw = covered_from
    stored = 0

    while True:
        body = client.call(
            "conversations.history",
            channel=channel_id,
            latest=floor_raw or None,
            inclusive=False,
            limit=HISTORY_PAGE_LIMIT,
        )
        page = body.get("messages", [])
        page_oldest = min((float(raw["ts"]) for raw in page), default=None)
        if page_oldest is None or (floor is not None and page_oldest >= floor):
            _finish_backfill(conn, workspace, channel_id)
            return stored

        for raw in page:
            _note_thread_parent(conn, workspace, channel_id, raw, thread_parents)
            stored += _store(conn, resolver, workspace, channel, raw)

        if floor is None:
            newest = max(page, key=lambda raw: float(raw["ts"]))
            conn.execute(
                "UPDATE sync_state SET last_ts = MAX(last_ts, ?),"
                " last_ts_raw = CASE WHEN ? > last_ts THEN ? ELSE last_ts_raw END,"
                " updated_at = ?"
                " WHERE workspace = ? AND channel_id = ?",
                (
                    float(newest["ts"]),
                    float(newest["ts"]),
                    newest["ts"],
                    time.time(),
                    workspace,
                    channel_id,
                ),
            )
        floor = page_oldest
        floor_raw = min(page, key=lambda raw: float(raw["ts"]))["ts"]
        conn.execute(
            "INSERT INTO backfill_state (workspace, channel_id, oldest_ts, oldest_ts_raw, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, channel_id) DO UPDATE SET"
            " oldest_ts = excluded.oldest_ts, oldest_ts_raw = excluded.oldest_ts_raw,"
            " updated_at = excluded.updated_at",
            (workspace, channel_id, floor, floor_raw, time.time()),
        )
        conn.commit()

        if not body.get("has_more", True):
            _finish_backfill(conn, workspace, channel_id)
            return stored

        # Checked after a committed page, never before one: a channel whose
        # budget is always spent by the time it is reached would otherwise make
        # no progress at all, run after run.
        if now() >= deadline:
            return stored


def _sweep_threads(
    conn, client, resolver, workspace: str, channel, thread_parents, now, deadline, report
) -> int:
    """Re-read threads whose parents the channel cursor has already passed.

    Reply timestamps deliberately never touch `sync_state.last_ts`: a reply can
    be newer than main-channel messages this run has not fetched yet, and
    letting it raise the cursor would skip straight over them.
    """
    if now() >= deadline:
        return 0

    channel_id = channel["id"]
    window_start = now() - THREAD_SWEEP_WINDOW_SECONDS
    due = _thread_parents_due_for_sweep(
        conn, workspace, channel_id, window_start, exclude=thread_parents
    )

    stored = 0
    for parent_ts in sorted(thread_parents | due):
        if now() >= deadline:
            break
        try:
            seen = []
            for raw in client.paginate(
                "conversations.replies", "messages", channel=channel_id, ts=parent_ts
            ):
                if _is_storable(raw):
                    seen.append(raw["ts"])
                stored += _store(conn, resolver, workspace, channel, raw)
        except AuthError:
            raise
        except SlackError as error:
            if error.code == "thread_not_found":
                # The parent is gone from Slack, so the thread is gone here too.
                # This is the one deletion the API actually lets us observe.
                _forget_thread(conn, workspace, channel_id, parent_ts)
                continue
            # One unreachable thread must not mark the whole channel failed —
            # that would fail every run, for every channel holding it. It is
            # still reported, and the run still exits non-zero.
            report.threads_failed.append((channel_id, parent_ts, error.code))
            continue
        _reconcile_thread(conn, workspace, channel_id, parent_ts, seen)
        _mark_thread_swept(conn, workspace, channel_id, parent_ts, now())
    conn.commit()
    return stored


def _note_thread_parent(conn, workspace: str, channel_id: str, raw: dict, thread_parents: set[str]) -> None:
    """Record a thread parent the moment it is seen, not at the end of the run.

    A local set is not enough: during backfill the history budget is always
    exhausted, so the sweep never runs in that pass, and a parent discovered
    there would be dropped with the set. Its replies are then unrecoverable —
    a backfilled thread with no stored replies has MAX(ts) equal to the
    parent's own ts, which leaves the recency window forever.
    """
    if not raw.get("reply_count"):
        return
    parent_ts = raw.get("thread_ts") or raw["ts"]
    thread_parents.add(parent_ts)
    conn.execute(
        "INSERT INTO thread_sweeps (workspace, channel_id, thread_ts, thread_ts_raw, last_swept, swept)"
        " VALUES (?, ?, ?, ?, 0, 0)"
        " ON CONFLICT (workspace, channel_id, thread_ts) DO NOTHING",
        (workspace, channel_id, float(parent_ts), parent_ts),
    )


def _forget_thread(conn, workspace: str, channel_id: str, parent_ts: str) -> None:
    conn.execute(
        "DELETE FROM messages WHERE workspace = ? AND channel_id = ? AND thread_ts_raw = ?",
        (workspace, channel_id, parent_ts),
    )
    conn.execute(
        "DELETE FROM thread_sweeps WHERE workspace = ? AND channel_id = ? AND thread_ts = ?",
        (workspace, channel_id, float(parent_ts)),
    )


def _reconcile_thread(conn, workspace: str, channel_id: str, parent_ts: str, seen: list[str]) -> None:
    """Drop stored replies Slack no longer serves for this thread.

    `conversations.replies` returns the thread as it stands now, so a reply
    missing from a complete re-read was deleted. This is the only place the
    corpus can observe a deletion at all — see the note in the design report.
    """
    if parent_ts not in seen:
        # conversations.replies always leads with the parent. A read that does
        # not contain it is not a trustworthy picture of the thread, and
        # deleting against it would throw away messages that still exist.
        return
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT ts_raw FROM messages WHERE workspace = ? AND channel_id = ? AND thread_ts_raw = ?",
            (workspace, channel_id, parent_ts),
        )
    }
    for ts_raw in stored - set(seen):
        conn.execute(
            "DELETE FROM messages WHERE id = ?",
            (message_id(workspace, channel_id, ts_raw),),
        )


def _backfill_state(conn, workspace: str, channel_id: str) -> tuple[bool, tuple[float | None, str], bool]:
    """Whether the channel has been visited, its coverage floor, and whether
    backward paging is finished.

    Visited and floor are separate answers: a channel that was empty the first
    time it was read has been visited and has no floor, and reading the missing
    floor as "never visited" is what would strand it — a brand-new DM is empty
    at the moment it first appears, so that is the first conversation with a new
    colleague, not a curiosity.
    """
    row = conn.execute(
        "SELECT oldest_ts, oldest_ts_raw, complete FROM backfill_state"
        " WHERE workspace = ? AND channel_id = ?",
        (workspace, channel_id),
    ).fetchone()
    if row is None:
        return False, (None, ""), False
    floor = row["oldest_ts"]
    floor_raw = row["oldest_ts_raw"] or (f"{floor}" if floor is not None else "")
    return True, (floor, floor_raw), bool(row["complete"])


def _finish_backfill(conn, workspace: str, channel_id: str) -> None:
    conn.execute(
        "INSERT INTO backfill_state (workspace, channel_id, complete, updated_at)"
        " VALUES (?, ?, 1, ?)"
        " ON CONFLICT (workspace, channel_id) DO UPDATE SET"
        " complete = 1, updated_at = excluded.updated_at",
        (workspace, channel_id, time.time()),
    )
    conn.commit()


def _thread_parents_due_for_sweep(
    conn: sqlite3.Connection,
    workspace: str,
    channel_id: str,
    window_start: float,
    *,
    exclude: set[str],
) -> set[str]:
    """Thread parents already in the corpus, so a reply landing on a thread the
    cursor has already passed still gets swept eventually.

    Windows on each thread's most recent known message, not the parent's own
    timestamp — a thread started outside the window can still be very much
    alive, and a thread that started inside it can already be dead. Capped and
    ordered least-recently-swept first so a channel with more live threads
    than the cap allows covers its backlog gradually across runs instead of
    either skipping most of it or blowing the run's budget on one channel.
    """
    rows = conn.execute(
        "WITH active AS ("
        "   SELECT thread_ts_raw AS raw, MAX(ts) AS latest FROM messages"
        "   WHERE workspace = ? AND channel_id = ? AND thread_ts_raw <> ''"
        "   GROUP BY thread_ts_raw"
        " ), known AS ("
        "   SELECT thread_ts_raw AS raw, last_swept, swept FROM thread_sweeps"
        "   WHERE workspace = ? AND channel_id = ? AND thread_ts_raw <> ''"
        " ), merged AS ("
        "   SELECT k.raw AS raw, k.last_swept AS last_swept, k.swept AS swept, a.latest AS latest"
        "     FROM known k LEFT JOIN active a ON a.raw = k.raw"
        "   UNION"
        "   SELECT a.raw, COALESCE(k.last_swept, 0), COALESCE(k.swept, 0), a.latest"
        "     FROM active a LEFT JOIN known k ON k.raw = a.raw"
        " )"
        " SELECT raw FROM merged"
        " WHERE swept = 0 OR (latest IS NOT NULL AND latest >= ?)"
        " ORDER BY swept ASC, last_swept ASC, raw ASC"
        " LIMIT ?",
        (workspace, channel_id, workspace, channel_id, window_start, THREAD_SWEEP_PER_CHANNEL_CAP),
    ).fetchall()
    return {row["raw"] for row in rows} - exclude


def _mark_thread_swept(conn: sqlite3.Connection, workspace: str, channel_id: str, thread_ts: str, swept_at: float) -> None:
    conn.execute(
        "INSERT INTO thread_sweeps (workspace, channel_id, thread_ts, thread_ts_raw, last_swept, swept)"
        " VALUES (?, ?, ?, ?, ?, 1)"
        " ON CONFLICT (workspace, channel_id, thread_ts) DO UPDATE SET"
        " last_swept = excluded.last_swept, thread_ts_raw = excluded.thread_ts_raw, swept = 1",
        (workspace, channel_id, float(thread_ts), thread_ts, swept_at),
    )


def _is_storable(raw: dict) -> bool:
    return raw.get("subtype") not in SKIP_SUBTYPES and bool(raw.get("text"))


def _store(conn, resolver: Resolver, workspace: str, channel, raw: dict) -> int:
    if not _is_storable(raw):
        return 0

    author_id = raw.get("user") or raw.get("bot_id", "")
    channel_name, kind = resolver.channel(channel["id"])
    identifier = message_id(workspace, channel["id"], raw["ts"])
    meta = {
        key: raw[key]
        for key in ("reactions", "files", "edited", "subtype", "reply_count")
        if key in raw
    }

    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts, ts_raw,"
        " thread_ts, thread_ts_raw, author_id, author_name, is_me, text, permalink, meta)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET text = excluded.text, meta = excluded.meta,"
        " ts_raw = excluded.ts_raw, thread_ts_raw = excluded.thread_ts_raw,"
        " thread_ts = excluded.thread_ts",
        (
            identifier,
            workspace,
            channel["id"],
            channel_name,
            kind,
            float(raw["ts"]),
            raw["ts"],
            float(raw["thread_ts"]) if raw.get("thread_ts") else None,
            raw.get("thread_ts") or "",
            author_id,
            resolver.user_name(author_id),
            1 if author_id == resolver.me_id else 0,
            raw["text"],
            _permalink(workspace, channel["id"], raw["ts"]),
            json.dumps(meta),
        ),
    )
    return 1


def _permalink(workspace: str, channel_id: str, ts: str) -> str:
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def _mark(conn, workspace: str, channel_id: str, status: str, detail: str) -> None:
    conn.execute(
        "UPDATE sync_state SET status = ?, detail = ?, updated_at = ?"
        " WHERE workspace = ? AND channel_id = ?",
        (status, detail, time.time(), workspace, channel_id),
    )
