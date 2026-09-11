"""Message ingest: tiered backfill, per-channel cursors, thread sweep.

Slack's channel cursor does not advance when a reply lands on an old thread
parent, so replies need their own sweep or they are lost forever — the single
most likely way for this corpus to go quietly incomplete.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from sherpa.corpus.directory import Resolver, sync_directory, tier_for
from sherpa.corpus.slack import AuthError, SlackClient, SlackError

SKIP_SUBTYPES = {"tombstone", "channel_join", "channel_leave", "message_deleted"}

# Bounded so a large corpus does not re-fetch every thread it has ever seen on every run.
THREAD_SWEEP_WINDOW_SECONDS = 30 * 24 * 60 * 60


@dataclass
class IngestReport:
    workspace: str
    messages: int = 0
    channels_done: int = 0
    channels_failed: list[tuple[str, str]] = field(default_factory=list)


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
    deadline = now() + budget_seconds

    for channel in _channels_in_tier_order(conn, workspace):
        if now() >= deadline:
            break
        try:
            report.messages += _ingest_channel(conn, client, resolver, workspace, channel, now, deadline)
            report.channels_done += 1
            _mark(conn, workspace, channel["id"], "ok", "")
        except AuthError:
            raise
        except SlackError as error:
            report.channels_failed.append((channel["id"], error.code))
            _mark(conn, workspace, channel["id"], "failed", error.code)

    conn.commit()
    return report


def _channels_in_tier_order(conn: sqlite3.Connection, workspace: str) -> list[sqlite3.Row]:
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
    for channel in channels:
        conn.execute(
            "INSERT INTO sync_state (workspace, channel_id, tier) VALUES (?, ?, ?)"
            " ON CONFLICT (workspace, channel_id) DO UPDATE SET tier = excluded.tier",
            (workspace, channel["id"], tier_for(channel, channel["id"] in posted_in)),
        )
    conn.commit()
    return sorted(channels, key=lambda c: tier_for(c, c["id"] in posted_in))


def _ingest_channel(conn, client, resolver, workspace: str, channel, now, deadline) -> int:
    channel_id = channel["id"]
    state = conn.execute(
        "SELECT last_ts FROM sync_state WHERE workspace = ? AND channel_id = ?",
        (workspace, channel_id),
    ).fetchone()
    oldest = state["last_ts"] if state else 0.0

    stored = 0
    newest = oldest
    thread_parents: set[str] = set()

    for raw in client.paginate(
        "conversations.history",
        "messages",
        channel=channel_id,
        oldest=f"{oldest}" if oldest else None,
        inclusive=False,
    ):
        newest = max(newest, float(raw["ts"]))
        if raw.get("reply_count"):
            thread_parents.add(raw.get("thread_ts", raw["ts"]))
        stored += _store(conn, resolver, workspace, channel, raw)

    window_start = now() - THREAD_SWEEP_WINDOW_SECONDS
    thread_parents |= _recently_active_thread_parents(conn, workspace, channel_id, window_start)

    for parent_ts in sorted(thread_parents):
        if now() >= deadline:
            break
        for raw in client.paginate(
            "conversations.replies", "messages", channel=channel_id, ts=parent_ts
        ):
            newest = max(newest, float(raw["ts"]))
            stored += _store(conn, resolver, workspace, channel, raw)

    conn.execute(
        "UPDATE sync_state SET last_ts = ?, updated_at = ? WHERE workspace = ? AND channel_id = ?",
        (newest, time.time(), workspace, channel_id),
    )
    conn.commit()
    return stored


def _recently_active_thread_parents(
    conn: sqlite3.Connection, workspace: str, channel_id: str, window_start: float
) -> set[str]:
    """Thread parents already in the corpus, so a reply landing on a thread the
    cursor has already passed still gets swept on the next run."""
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT thread_ts FROM messages"
            " WHERE workspace = ? AND channel_id = ? AND thread_ts IS NOT NULL AND thread_ts >= ?",
            (workspace, channel_id, window_start),
        )
    }


def _store(conn, resolver: Resolver, workspace: str, channel, raw: dict) -> int:
    if raw.get("subtype") in SKIP_SUBTYPES or not raw.get("text"):
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
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " thread_ts, author_id, author_name, is_me, text, permalink, meta)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET text = excluded.text, meta = excluded.meta",
        (
            identifier,
            workspace,
            channel["id"],
            channel_name,
            kind,
            float(raw["ts"]),
            float(raw["thread_ts"]) if raw.get("thread_ts") else None,
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
