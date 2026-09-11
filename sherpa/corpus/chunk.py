"""Conversation windowing — the unit of embedding.

A single Slack message is too short and too context-dependent to embed on its
own: "yeah I'll get that to you friday" carries almost no standalone semantic
signal, and it is exactly the message these queries need to find. So the corpus
embeds conversation windows, formatted as a script and prefixed with a
deterministic context header.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

GAP_SECONDS = 1800
MAX_MESSAGES = 40
OVERLAP = 2
MAX_CHARS = 6000


def format_script(messages) -> str:
    return "\n".join(f"{m['author_name']}: {m['text']}" for m in messages)


def build_header(channel_name: str, kind: str, participants: list[str], start_ts: float) -> str:
    when = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    people = ", ".join(participants)
    if kind == "im":
        return f"DM between {people}, {when}"
    if kind == "mpim":
        return f"Group DM between {people}, {when}"
    return f"#{channel_name}, {when}, participants: {people}"


def rechunk_all(conn: sqlite3.Connection, workspace: str) -> int:
    channels = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE workspace = ?", (workspace,)
        )
    ]
    return sum(build_chunks(conn, workspace, channel_id) for channel_id in channels)


def build_chunks(conn: sqlite3.Connection, workspace: str, channel_id: str) -> int:
    messages = conn.execute(
        "SELECT * FROM messages WHERE workspace = ? AND channel_id = ? ORDER BY ts",
        (workspace, channel_id),
    ).fetchall()
    if not messages:
        return 0

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

    written = 0
    for window in windows:
        if window:
            written += _write(conn, workspace, channel_id, window)
    conn.commit()
    return written


def _window(messages: list) -> list[list]:
    """Split loose channel messages on silence or length, with overlap."""
    if not messages:
        return []

    windows, current = [], [messages[0]]
    for previous, message in zip(messages, messages[1:]):
        too_quiet = message["ts"] - previous["ts"] > GAP_SECONDS
        too_long = len(current) >= MAX_MESSAGES
        if too_quiet or too_long:
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


def _write(conn: sqlite3.Connection, workspace: str, channel_id: str, window: list) -> int:
    text = format_script(window)
    participants = sorted({m["author_name"] for m in window})
    header = build_header(
        window[0]["channel_name"], window[0]["channel_kind"], participants, window[0]["ts"]
    )
    content_hash = hashlib.sha256(f"{header}\n{text}".encode()).hexdigest()

    existing = conn.execute(
        "SELECT id, content_hash FROM chunks"
        " WHERE workspace = ? AND channel_id = ? AND msg_first_id = ? AND msg_last_id = ?",
        (workspace, channel_id, window[0]["id"], window[-1]["id"]),
    ).fetchone()

    if existing and existing["content_hash"] == content_hash:
        return 0  # unchanged; leave `embedded` alone so it is not re-embedded

    if existing:
        conn.execute("DELETE FROM chunks WHERE id = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (existing["id"],))
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (existing["id"],))

    cursor = conn.execute(
        "INSERT INTO chunks (workspace, channel_id, start_ts, end_ts, thread_ts, participants,"
        " msg_first_id, msg_last_id, header, text, content_hash, embedded)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            workspace,
            channel_id,
            window[0]["ts"],
            window[-1]["ts"],
            window[0]["thread_ts"],
            ", ".join(participants),
            window[0]["id"],
            window[-1]["id"],
            header,
            text,
            content_hash,
        ),
    )
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, header, text, participants) VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, header, text, ", ".join(participants)),
    )
    return 1
