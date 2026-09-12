"""Conversation windowing — the unit of embedding.

A single Slack message is too short and too context-dependent to embed on its
own: "yeah I'll get that to you friday" carries almost no standalone semantic
signal, and it is exactly the message these queries need to find. So the corpus
embeds conversation windows, formatted as a script and prefixed with a
deterministic context header.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

# Three ways a message is machine-generated: an app user listed by users.list
# (Jira, Optimus), a bot id Slack never lists at all, and the bot_id the
# payload itself carries.
def human_authored(message: str = "m", user: str = "u") -> str:
    """SQL for "this message was written by a person", over the given aliases.

    One definition, used by the chunker to decide what becomes a retrieval unit
    and by the sweep to decide which threads to read first. A second copy of it
    would drift.
    """
    return (
        f"COALESCE({user}.is_bot, 0) = 0"
        f" AND {message}.author_id NOT LIKE 'B%'"
        f" AND json_extract({message}.meta, '$.bot_id') IS NULL"
    )


HUMAN_AUTHORED = human_authored()

GAP_SECONDS = 1800
MAX_MESSAGES = 40
OVERLAP = 2
MAX_CHARS = 6000


def format_script(messages) -> str:
    return "\n".join(f"{m['author_name']}: {m['text']}" for m in messages)


def _join_names(names: list[str]) -> str:
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def build_header(channel_name: str, kind: str, participants: list[str], start_ts: float) -> str:
    when = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if kind == "im":
        return f"DM between {_join_names(participants)}, {when}"
    if kind == "mpim":
        return f"Group DM between {_join_names(participants)}, {when}"
    return f"#{channel_name}, {when}, participants: {', '.join(participants)}"


def roster(conn: sqlite3.Connection, workspace: str, channel_id: str) -> list[str]:
    """Everyone in the conversation, from channel membership rather than speech.

    A DM or group DM is *with* people whether or not they have answered yet, and
    the unanswered case is precisely the one `owed` exists to surface. Named
    channels are left out on purpose: their membership runs to thousands and is
    not what "a conversation with Hunter" means -- there, presence comes from
    speaking or being mentioned in the window itself.
    """
    channel = conn.execute(
        "SELECT name, kind, member_ids FROM channels WHERE workspace = ? AND id = ?",
        (workspace, channel_id),
    ).fetchone()
    if channel is None or channel["kind"] not in ("im", "mpim"):
        return []

    names = {part.strip() for part in channel["name"].split(",") if part.strip()}
    try:
        member_ids = json.loads(channel["member_ids"])
    except (TypeError, ValueError):
        member_ids = []
    if member_ids:
        placeholders = ",".join("?" * len(member_ids))
        names.update(
            row[0]
            for row in conn.execute(
                f"SELECT name FROM users WHERE workspace = ? AND id IN ({placeholders})",
                (workspace, *member_ids),
            )
        )
    return sorted(names)


def rechunk_all(conn: sqlite3.Connection, workspace: str) -> int:
    """Rebuild every channel. A whole-workspace pass -- for a one-off repair,
    not for the cron path, which rebuilds only what changed."""
    channels = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE workspace = ?", (workspace,)
        )
    ]
    return rechunk_channels(conn, workspace, channels)


def rechunk_channels(conn: sqlite3.Connection, workspace: str, channel_ids) -> int:
    """Rebuild exactly the named channels.

    Re-windowing the whole workspace on every cron run fully materializes every
    message in it -- 500k at the design's sizing -- to rediscover that almost
    nothing moved, which is the work `content_hash` exists to avoid. A channel
    that took no messages this run cannot have produced a different windowing.
    """
    return sum(build_chunks(conn, workspace, channel_id) for channel_id in sorted(channel_ids))


def rechunk_dirty(conn: sqlite3.Connection, workspace: str) -> int:
    """Rebuild every channel the database says is out of date, then clear it.

    The dirty flag is written alongside the messages and cleared only after the
    rebuild, so a run that aborts between the two -- a rejected token, a
    rate-limit exhaustion, a kill -- leaves the work outstanding for the next
    run instead of leaving messages that nothing will ever chunk.
    """
    from sherpa.corpus.ingest import clear_dirty, dirty_channels

    rebuilt = 0
    for channel_id in dirty_channels(conn, workspace):
        rebuilt += build_chunks(conn, workspace, channel_id)
        clear_dirty(conn, workspace, channel_id)
    conn.commit()
    return rebuilt


def build_chunks(conn: sqlite3.Connection, workspace: str, channel_id: str) -> int:
    """Make the stored chunks for one channel exactly match its current windowing.

    Windows are keyed by (msg_first_id, msg_last_id). An unchanged window's row is
    left untouched — including its `embedded` flag, so Task 8 does not re-embed it.
    A changed or new window is (re)written. Any stored chunk whose key is no longer
    produced (e.g. a still-open window that grew because new messages arrived) is
    deleted, along with its `chunks_fts` and `chunks_vec` rows, so no orphan survives
    incremental ingestion. Returns the number of chunks now stored for the channel —
    the size of the resulting authoritative set, not a count of writes.
    """
    messages = conn.execute(
        # Bot output is stored but never chunked: alerts are formulaic, so they
        # saturate BM25 and cluster tightly in embedding space, crowding out the
        # conversations this corpus exists to find -- and they cannot contain a
        # commitment. Nothing is discarded; a later rechunk can bring them back.
        "SELECT m.* FROM messages m"
        " LEFT JOIN users u ON u.workspace = m.workspace AND u.id = m.author_id"
        " WHERE m.workspace = ? AND m.channel_id = ?"
        f" AND {HUMAN_AUTHORED}"
        " ORDER BY m.ts",
        (workspace, channel_id),
    ).fetchall()

    threads: dict[float, list] = {}
    loose = []
    for message in messages:
        if message["thread_ts"]:
            threads.setdefault(message["thread_ts"], []).append(message)
        else:
            loose.append(message)

    windows = _window(loose)
    for thread_messages in threads.values():
        windows.extend(_split_oversized(thread_messages))

    members = roster(conn, workspace, channel_id)
    me = _me_name(conn, workspace)

    current_keys = set()
    for window in windows:
        if window:
            current_keys.add((window[0]["id"], window[-1]["id"]))
            _write(conn, workspace, channel_id, window, members, me)

    _delete_stale(conn, workspace, channel_id, current_keys)
    conn.commit()
    return len(current_keys)


def _delete_stale(
    conn: sqlite3.Connection, workspace: str, channel_id: str, keep_keys: set[tuple[str, str]]
) -> None:
    stale = [
        row["id"]
        for row in conn.execute(
            "SELECT id, msg_first_id, msg_last_id FROM chunks WHERE workspace = ? AND channel_id = ?",
            (workspace, channel_id),
        )
        if (row["msg_first_id"], row["msg_last_id"]) not in keep_keys
    ]
    for chunk_id in stale:
        conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))


def _window(messages: list) -> list[list]:
    """Split loose channel messages on silence or length, with overlap."""
    if not messages:
        return []

    windows, current = [], [messages[0]]
    for previous, message in zip(messages, messages[1:]):
        too_quiet = message["ts"] - previous["ts"] > GAP_SECONDS
        too_long = len(current) >= MAX_MESSAGES
        # Forty messages of pasted logs is 100k characters. ollama truncates
        # past the model's context without saying so, the chunk is still marked
        # embedded, and `status` calls the corpus healthy while the vector
        # stands for the first few messages only.
        too_big = len(format_script(current)) >= MAX_CHARS
        if too_quiet or too_long or too_big:
            windows.append(current)
            current = current[-OVERLAP:] + [message]
        else:
            current.append(message)
    windows.append(current)
    return windows


def _split_oversized(messages: list) -> list[list]:
    """A thread is one chunk unless it is too big, then split by count with overlap."""
    if len(messages) <= MAX_MESSAGES and len(format_script(messages)) <= MAX_CHARS:
        return [messages]

    windows, current = [], []
    for message in messages:
        current.append(message)
        if len(current) >= MAX_MESSAGES or len(format_script(current)) > MAX_CHARS:
            windows.append(current)
            current = current[-OVERLAP:]
    if len(current) > OVERLAP or not windows:
        windows.append(current)
    return windows


def _mentioned(window: list) -> set[str]:
    """People the window names without speaking.

    In a named channel, being @-mentioned is how someone is present in a
    conversation at all -- and an unanswered "@Hunter I'll get you the schema"
    is the exact shape `owed hunter` has to find.
    """
    names: set[str] = set()
    for message in window:
        try:
            meta = json.loads(message["meta"])
        except (TypeError, ValueError, IndexError):
            continue
        names.update(meta.get("mentions", []))
    return names


def _me_name(conn: sqlite3.Connection, workspace: str) -> str:
    row = conn.execute(
        "SELECT name FROM users WHERE workspace = ? AND is_me = 1", (workspace,)
    ).fetchone()
    return row["name"] if row else ""


def _write(
    conn: sqlite3.Connection,
    workspace: str,
    channel_id: str,
    window: list,
    members: list[str],
    me: str,
) -> int:
    text = format_script(window)
    speakers = sorted({m["author_name"] for m in window})
    everyone = sorted(set(speakers) | set(members) | _mentioned(window))
    if me in everyone:
        everyone = [me] + [name for name in everyone if name != me]
    people = ", ".join(everyone)
    header = build_header(
        window[0]["channel_name"], window[0]["channel_kind"], everyone, window[0]["ts"]
    )
    content_hash = hashlib.sha256(f"{header}\n{people}\n{text}".encode()).hexdigest()

    existing = conn.execute(
        "SELECT id, content_hash FROM chunks"
        " WHERE workspace = ? AND channel_id = ? AND msg_first_id = ? AND msg_last_id = ?",
        (workspace, channel_id, window[0]["id"], window[-1]["id"]),
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        return 0  # unchanged; leave `embedded` alone so it is not re-embedded

    if existing:
        conn.execute("DELETE FROM chunks WHERE id = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (existing["id"],))

    cursor = conn.execute(
        "INSERT INTO chunks (workspace, channel_id, start_ts, end_ts, thread_ts, participants,"
        " people, msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            workspace,
            channel_id,
            window[0]["ts"],
            window[-1]["ts"],
            window[0]["thread_ts"],
            ", ".join(speakers),
            people,
            window[0]["id"],
            window[-1]["id"],
            header,
            text,
            content_hash,
        ),
    )
    conn.execute(
        "INSERT INTO chunks_fts (rowid, chunk_id, header, text, participants)"
        " VALUES (?, ?, ?, ?, ?)",
        (cursor.lastrowid, cursor.lastrowid, header, text, people),
    )
    return 1
