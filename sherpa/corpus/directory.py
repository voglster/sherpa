"""Users and channels: fetch, store, and resolve IDs to human names.

Resolution happens at ingest time so the corpus holds "Hunter Reeves", not
U03K2. An agent searching for a person must not need to know their Slack ID.
"""

from __future__ import annotations

import json
import sqlite3

from sherpa.corpus.slack import SlackClient


def sync_directory(conn: sqlite3.Connection, client: SlackClient, workspace: str, me: str | None) -> str:
    me_id = me or client.call("auth.test")["user_id"]

    for member in client.paginate("users.list", "members"):
        profile = member.get("profile", {})
        conn.execute(
            "INSERT INTO users (workspace, id, name, display_name, is_me)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, display_name = excluded.display_name, is_me = excluded.is_me",
            (
                workspace,
                member["id"],
                profile.get("real_name") or member.get("name") or member["id"],
                profile.get("display_name", ""),
                1 if member["id"] == me_id else 0,
            ),
        )

    for channel in client.paginate(
        "conversations.list",
        "channels",
        types="public_channel,private_channel,mpim,im",
        exclude_archived=True,
    ):
        kind = _kind(channel)
        conn.execute(
            "INSERT INTO channels (workspace, id, name, kind, is_member, member_ids)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, kind = excluded.kind, is_member = excluded.is_member",
            (
                workspace,
                channel["id"],
                _name(channel, kind),
                kind,
                1 if channel.get("is_member") or kind in ("im", "mpim") else 0,
                json.dumps(channel.get("members", [])),
            ),
        )

    conn.commit()
    return me_id


def _kind(channel: dict) -> str:
    if channel.get("is_im"):
        return "im"
    if channel.get("is_mpim"):
        return "mpim"
    return "private" if channel.get("is_private") else "public"


def _name(channel: dict, kind: str) -> str:
    if kind == "im":
        return channel.get("user", channel["id"])
    return channel.get("name", channel["id"])


def tier_for(channel_row, has_posted: bool) -> int:
    """Backfill priority: DMs first, then channels Jim has spoken in, then the rest."""
    if channel_row["kind"] in ("im", "mpim"):
        return 1
    return 2 if has_posted else 3


class Resolver:
    """ID to human name, cached per workspace for the life of one sync run."""

    def __init__(self, conn: sqlite3.Connection, workspace: str) -> None:
        self._users = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM users WHERE workspace = ?", (workspace,))
        }
        self._channels = {
            row["id"]: (row["name"], row["kind"])
            for row in conn.execute(
                "SELECT id, name, kind FROM channels WHERE workspace = ?", (workspace,)
            )
        }
        me = conn.execute(
            "SELECT id FROM users WHERE workspace = ? AND is_me = 1", (workspace,)
        ).fetchone()
        self.me_id = me["id"] if me else ""

    def user_name(self, user_id: str) -> str:
        return self._users.get(user_id, user_id)

    def channel(self, channel_id: str) -> tuple[str, str]:
        name, kind = self._channels.get(channel_id, (channel_id, "public"))
        return (self.user_name(name), kind) if kind == "im" else (name, kind)
