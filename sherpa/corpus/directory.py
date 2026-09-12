"""Users and channels: fetch, store, and resolve IDs to human names.

Resolution happens at ingest time so the corpus holds "Hunter Reeves", not
U03K2. An agent searching for a person must not need to know their Slack ID.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3

import time as _time

from sherpa.corpus.slack import SlackClient, SlackError, AuthError

# Group DM membership barely moves, and the real workspace has 1,382 of them.
# Re-reading all of it every quarter hour cost about seven minutes before a
# single message was fetched, which made --budget meaningless.
MEMBERS_REFRESH_SECONDS = 24 * 60 * 60
MEMBERS_PER_RUN = 100

# Slack's message-text markup. Every form carries an ID and an optional display
# label after a pipe; the label is authoritative when present, since it is what
# the sender saw.
_USER_REF = re.compile(r"<@(?P<id>[UWB][A-Z0-9]*)(?:\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#(?P<id>[CGD][A-Z0-9]*)(?:\|(?P<label>[^>]*))?>")
_SUBTEAM_REF = re.compile(r"<!subteam\^(?P<id>[A-Z0-9]+)(?:\|(?P<label>[^>]*))?>")
_SPECIAL_REF = re.compile(r"<!(?P<name>here|channel|everyone)(?:\|[^>]*)?>")
_LINK = re.compile(r"<(?P<url>(?:https?|mailto|tel):[^|>]+)(?:\|(?P<label>[^>]*))?>")


def _render_link(match: re.Match) -> str:
    url, label = match["url"], match["label"]
    if url.startswith("mailto:"):
        return label or url[len("mailto:"):]
    if not label or label == url:
        return url
    return f"{label} ({url})"



def team_host(conn: sqlite3.Connection, workspace: str) -> str:
    """The workspace's real Slack host, or the config name as a last resort.

    The fallback is the old behavior: wrong for a renamed workspace, but a
    well-formed URL, which is better than emitting something malformed for a
    row written before the host was known.
    """
    row = conn.execute(
        "SELECT team_host FROM workspaces WHERE name = ?", (workspace,)
    ).fetchone()
    return (row["team_host"] if row and row["team_host"] else f"{workspace}.slack.com")


def _host_of(url: str | None) -> str:
    """`https://capspire.slack.com/` -> `capspire.slack.com`."""
    if not url:
        return ""
    return url.split("://", 1)[-1].strip("/").split("/", 1)[0]


def _learn_team_host(conn: sqlite3.Connection, workspace: str, url: str | None) -> None:
    host = _host_of(url)
    if not host:
        return
    previous = team_host(conn, workspace)
    conn.execute(
        "INSERT INTO workspaces (name, team_host) VALUES (?, ?)"
        " ON CONFLICT (name) DO UPDATE SET team_host = excluded.team_host",
        (workspace, host),
    )
    if previous != host:
        # Everything already stored points at the old host. Permalinks live only
        # on `messages` (chunks hydrate through them), so this is the whole repair.
        conn.execute(
            "UPDATE messages SET permalink = replace(permalink, ?, ?) WHERE workspace = ?",
            (previous, host, workspace),
        )


def sync_directory(
    conn: sqlite3.Connection,
    client: SlackClient,
    workspace: str,
    me: str | None,
    *,
    now=_time.time,
    deadline: float | None = None,
) -> str:
    # Called even when `me` is configured: it is the only source of the team's
    # real host, and one request per workspace per run is nothing next to the
    # thousands ingest makes -- while skipping it would pin the host at whatever
    # it was the day the corpus was created, through any later rename.
    identity = client.call("auth.test")
    me_id = me or identity["user_id"]
    _learn_team_host(conn, workspace, identity.get("url"))

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
    known = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, member_ids, members_fetched_at FROM channels WHERE workspace = ?",
            (workspace,),
        )
    }
    listed: list[str] = []
    stale: list[str] = []
    # One reading of the clock for the whole walk: which memberships are due is
    # a question about the run, not about each channel's microsecond.
    walked_at = now()

    # The listing walk still runs in full every time: it is what catches new
    # channels and, per the departure rule, channels that have gone. Only the
    # per-channel membership reads became incremental.
    for channel in client.paginate(
        "conversations.list",
        "channels",
        types="public_channel,private_channel,mpim,im",
        exclude_archived=True,
    ):
        kind = _kind(channel)
        stored = known.get(channel["id"])
        member_ids, fetched_at = _known_members(stored)

        if kind == "im":
            # Both parties are already in hand: me, and the user the listing
            # names. 254 requests a run for something nobody had to ask for.
            member_ids = [me_id, channel.get("user")] if channel.get("user") else member_ids
            fetched_at = walked_at
        elif kind == "mpim" and _members_due(fetched_at, walked_at):
            stale.append(channel["id"])

        conn.execute(
            "INSERT INTO channels (workspace, id, name, kind, is_member, member_ids, members_fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (workspace, id) DO UPDATE SET"
            " name = excluded.name, kind = excluded.kind, is_member = excluded.is_member,"
            " member_ids = excluded.member_ids, members_fetched_at = excluded.members_fetched_at",
            (
                workspace,
                channel["id"],
                _resolve_channel_name(channel, kind, resolver, member_ids),
                kind,
                1 if channel.get("is_member") or kind in ("im", "mpim") else 0,
                json.dumps(member_ids),
                fetched_at,
            ),
        )
        listed.append(channel["id"])

    _mark_departures(conn, workspace, listed)
    _refresh_memberships(conn, client, resolver, workspace, stale, now, deadline)
    conn.commit()
    return me_id


def _mark_departures(conn: sqlite3.Connection, workspace: str, listed: list[str]) -> None:
    """Stop scheduling channels the listing no longer contains.

    A channel Jim leaves, or that gets archived, simply stops appearing. Left
    flagged as a member it is scheduled every run, fails every run, and `status`
    exits 2 forever -- so within a few weeks of ordinary churn the fail-loud
    signal is noise and gets ignored, which is how the corpus ends up silently
    incomplete by a different road. Only reached after the walk completed:
    a pagination error propagates instead, so a half-read listing never
    unsubscribes anything.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS listed_channels (id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM listed_channels")
    conn.executemany(
        "INSERT OR IGNORE INTO listed_channels (id) VALUES (?)", [(cid,) for cid in listed]
    )
    conn.execute(
        "UPDATE channels SET is_member = 0"
        " WHERE workspace = ? AND is_member = 1"
        " AND id NOT IN (SELECT id FROM listed_channels)",
        (workspace,),
    )
    conn.execute("DELETE FROM listed_channels")


def _known_members(stored) -> tuple[list, float]:
    if stored is None:
        return [], 0.0
    try:
        return json.loads(stored["member_ids"]), stored["members_fetched_at"]
    except (TypeError, ValueError):
        return [], stored["members_fetched_at"]


def _members_due(fetched_at: float, current: float) -> bool:
    return not fetched_at or (current - fetched_at) >= MEMBERS_REFRESH_SECONDS


def _refresh_memberships(
    conn: sqlite3.Connection,
    client: SlackClient,
    resolver: "_UserResolver",
    workspace: str,
    candidates: list[str],
    now,
    deadline: float | None,
) -> None:
    """Read group DM membership for the channels most overdue for it.

    Least-recently-read first, so a channel with none at all -- which is what
    names a group DM -- goes first and a workspace with thousands of them
    completes over the next few runs instead of blocking the first one for
    minutes. Bounded by both a per-run count and the run's own clock.
    """
    if not candidates:
        return

    ordered = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM channels WHERE workspace = ? AND id IN ({})"
            " ORDER BY members_fetched_at ASC, id ASC LIMIT ?".format(
                ",".join("?" * len(candidates))
            ),
            (workspace, *candidates, MEMBERS_PER_RUN),
        )
    ]

    for channel_id in ordered:
        member_ids = _fetch_member_ids({"id": channel_id}, "mpim", client)
        if member_ids:
            names = ", ".join(resolver.resolve(uid) for uid in member_ids)
            conn.execute(
                "UPDATE channels SET member_ids = ?, members_fetched_at = ?, name = ?"
                " WHERE workspace = ? AND id = ?",
                (json.dumps(member_ids), now(), names, workspace, channel_id),
            )
            conn.commit()
        # Checked after a completed read, so a workspace whose directory budget
        # is always spent still names one more group DM every run rather than
        # never naming any.
        if deadline is not None and now() >= deadline:
            return


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
        # Cache negative result: this prevents repeated users.info calls for the same
        # unresolvable user across multiple syncs. Self-heals for users who later appear
        # in users.list (overwritten in next sync's first loop), but persists as Unknown
        # for external Slack Connect users and deactivated accounts. This is a narrow
        # tradeoff acceptable for now.
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
    except AuthError:
        raise
    except SlackError:
        pass
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
        self.team_host = team_host(conn, workspace)

    def user_name(self, user_id: str) -> str:
        return self._users.get(user_id, user_id)

    def channel(self, channel_id: str) -> tuple[str, str]:
        name, kind = self._channels.get(channel_id, (channel_id, "public"))
        return (name, kind)

    def render(self, text: str) -> tuple[str, list[str]]:
        """Message text with every ID resolved, plus the people it mentions.

        Slack stores a mention as `<@U03K2ABCD>`, and in a channel that is the
        primary way a person's name appears at all. Left raw, the name reaches
        neither the FTS index nor the embedding, so "what did I promise Hunter"
        cannot find the message that says exactly that. Resolution happens here,
        at ingest, for the same reason author and channel names do.
        """
        mentioned: list[str] = []

        def user(match: re.Match) -> str:
            name = self.user_name(match["id"])
            if name not in mentioned:
                mentioned.append(name)
            return f"@{name}"

        def channel_ref(match: re.Match) -> str:
            return f"#{match['label'] or self.channel(match['id'])[0]}"

        rendered = _USER_REF.sub(user, text)
        rendered = _CHANNEL_REF.sub(channel_ref, rendered)
        rendered = _SUBTEAM_REF.sub(lambda m: m["label"] or f"@{m['id']}", rendered)
        rendered = _SPECIAL_REF.sub(lambda m: f"@{m['name']}", rendered)
        rendered = _LINK.sub(_render_link, rendered)
        rendered = html.unescape(rendered)
        return rendered, sorted(mentioned)
