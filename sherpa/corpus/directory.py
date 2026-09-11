"""Users and channels: fetch, store, and resolve IDs to human names.

Resolution happens at ingest time so the corpus holds "Hunter Reeves", not
U03K2. An agent searching for a person must not need to know their Slack ID.
"""

from __future__ import annotations

import json
import sqlite3

from sherpa.corpus.slack import SlackClient, SlackError, AuthError


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

    resolver = _UserResolver(conn, client, workspace)

    for channel in client.paginate(
        "conversations.list",
        "channels",
        types="public_channel,private_channel,mpim,im",
        exclude_archived=True,
    ):
        kind = _kind(channel)
        member_ids = _fetch_member_ids(channel, kind, client)
        name = _resolve_channel_name(channel, kind, resolver, member_ids)

        conn.execute(
            "INSERT INTO channels (workspace, id, name, kind, is_member, member_ids)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, kind = excluded.kind, is_member = excluded.is_member, member_ids = excluded.member_ids",
            (
                workspace,
                channel["id"],
                name,
                kind,
                1 if channel.get("is_member") or kind in ("im", "mpim") else 0,
                json.dumps(member_ids),
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


class _UserResolver:
    """Resolves user IDs to names, fetching via users.info when needed."""

    def __init__(self, conn: sqlite3.Connection, client: SlackClient, workspace: str) -> None:
        self.conn = conn
        self.client = client
        self.workspace = workspace
        self._cache = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM users WHERE workspace = ?", (workspace,))
        }

    def resolve(self, user_id: str) -> str:
        """Resolve user ID to name, fetching via users.info if not in cache."""
        if user_id in self._cache:
            return self._cache[user_id]

        try:
            user_info = self.client.call("users.info", user=user_id)
            if user_info.get("ok"):
                user = user_info["user"]
                profile = user.get("profile", {})
                name = profile.get("real_name") or user.get("name") or user_id
                self.conn.execute(
                    "INSERT INTO users (workspace, id, name, display_name, is_me)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (workspace, id) DO UPDATE SET"
                    " name = excluded.name, display_name = excluded.display_name",
                    (
                        self.workspace,
                        user_id,
                        name,
                        profile.get("display_name", ""),
                        0,
                    ),
                )
                self._cache[user_id] = name
                return name
        except AuthError:
            raise
        except SlackError:
            pass

        placeholder = f"Unknown user ({user_id})"
        self.conn.execute(
            "INSERT INTO users (workspace, id, name, display_name, is_me)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name",
            (
                self.workspace,
                user_id,
                placeholder,
                "",
                0,
            ),
        )
        self._cache[user_id] = placeholder
        return placeholder


def _resolve_channel_name(channel: dict, kind: str, resolver: _UserResolver, member_ids: list) -> str:
    """Resolve channel name at write time, handling DM and group DM naming."""
    if kind == "im":
        user_id = channel.get("user", channel["id"])
        return resolver.resolve(user_id)
    elif kind == "mpim":
        if member_ids:
            names = [resolver.resolve(uid) for uid in member_ids]
            return ", ".join(names)
        return channel.get("name", channel["id"])
    else:
        return channel.get("name", channel["id"])


def _fetch_member_ids(channel: dict, kind: str, client: SlackClient) -> list:
    """Fetch member IDs for im and mpim channels only."""
    if kind not in ("im", "mpim"):
        return []

    members = []
    try:
        for member in client.paginate("conversations.members", "members", channel=channel["id"]):
            members.append(member)
    except SlackError:
        pass
    except AuthError:
        raise
    return members


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
        return (name, kind)
