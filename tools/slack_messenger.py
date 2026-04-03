#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: slack_messenger
description: Send Slack messages to channels and DMs. Supports @(name) for user mentions and auto-links Jira keys. Fuzzy user/channel lookup with local caching.
categories: [slack, messaging, communication]
secrets:
  - SLACK_USER_TOKEN
usage: |
  send --channel <name> --text 'Hello team!'
  send --channel-id C01ABC23DEF --file /tmp/msg.txt
  send --channel general --stdin < message.txt
  send --channel general --text 'Hey @(jane doe) check this out'
  send --channel general --text 'reply' --thread 1774551827.458609
  send --channel general --blocks /tmp/blocks.json --text 'fallback'
  dm --user <name> --text 'Hey, quick question...'
  dm --user-id U01ABC23DEF --file /tmp/msg.txt
  channels [--filter general] [--refresh]
  users [--filter swap] [--refresh]
notes: |
  @(name) in message text becomes a Slack @mention. Errors if ambiguous (e.g. multiple "nathan"s).
  Jira ticket keys (e.g. KB-123) are auto-linked. Channel/user lookups are cached locally.
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
CACHE_DIR = Path.home() / ".sherpa" / "cache"
SLACK_ID_RE = re.compile(r"^[CGUWDBT][A-Z0-9]{8,}$")
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
MENTION_RE = re.compile(r"@\(([^)]+)\)")
RATE_LIMIT_THRESHOLD = 30  # seconds — auto-retry if Retry-After <= this


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _linkify_jira_keys(text: str) -> str:
    """Replace Jira ticket keys (e.g. KB-12345) with Slack-formatted links."""
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    jira_url = vault.get("JIRA_URL")
    if not jira_url:
        return text
    jira_url = jira_url.rstrip("/")
    return JIRA_KEY_RE.sub(lambda m: f"<{jira_url}/browse/{m.group(1)}|{m.group(1)}>", text)


# --- Cache helpers ---


def _load_cache(name: str) -> list[dict]:
    path = CACHE_DIR / f"slack_{name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_cache(name: str, data: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"slack_{name}.json").write_text(json.dumps(data))


def _invalidate_cache(name: str) -> None:
    path = CACHE_DIR / f"slack_{name}.json"
    if path.exists():
        path.unlink()


# --- Slack API helpers ---


def _handle_rate_limit(resp: httpx.Response) -> None:
    """Check for rate limiting and either sleep or bail with a message."""
    data = resp.json()
    if data.get("ok") or data.get("error") != "ratelimited":
        return
    retry_after = int(resp.headers.get("Retry-After", "60"))
    if retry_after <= RATE_LIMIT_THRESHOLD:
        print(f"Rate limited, retrying in {retry_after}s...", file=sys.stderr)
        raise _RateLimitRetry(retry_after)
    print(f"Rate limited by Slack. Try again in {retry_after} seconds.", file=sys.stderr)
    print(f"RETRY_AFTER:{retry_after}", file=sys.stderr)
    sys.exit(2)


class _RateLimitRetry(Exception):
    def __init__(self, wait: int):
        self.wait = wait


async def _slack_get(client: httpx.AsyncClient, headers: dict, url: str, params: dict | None = None) -> dict:
    while True:
        resp = await client.get(url, headers=headers, params=params or {})
        try:
            _handle_rate_limit(resp)
        except _RateLimitRetry as e:
            await asyncio.sleep(e.wait)
            continue
        data = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            print(f"Slack API error: {error}", file=sys.stderr)
            sys.exit(2)
        return data


async def _slack_post(client: httpx.AsyncClient, headers: dict, url: str, payload: dict) -> dict:
    while True:
        resp = await client.post(url, headers=headers, json=payload)
        try:
            _handle_rate_limit(resp)
        except _RateLimitRetry as e:
            await asyncio.sleep(e.wait)
            continue
        data = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            print(f"Slack API error: {error}", file=sys.stderr)
            sys.exit(2)
        return data


# --- Fetch-all helpers (populate cache) ---


async def _fetch_all_channels(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    results = []
    cursor = None
    while True:
        params = {"types": "public_channel,private_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/conversations.list", params)
        for ch in data.get("channels", []):
            results.append({
                "id": ch["id"],
                "name": ch.get("name"),
                "num_members": ch.get("num_members", 0),
            })
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache("channels", results)
    return results


async def _fetch_all_users(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    results = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/users.list", params)
        for user in data.get("members", []):
            if user.get("deleted") or user.get("is_bot"):
                continue
            results.append({
                "id": user["id"],
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "display_name": user.get("profile", {}).get("display_name"),
            })
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache("users", results)
    return results


# --- Resolve helpers (cache-first, invalidate on miss) ---


def _search_channels(channels: list[dict], query: str) -> dict | None:
    query_lower = query.lstrip("#").lower()
    for ch in channels:
        if query_lower in ch.get("name", "").lower():
            return ch
    return None


def _search_users(users: list[dict], query: str) -> dict | None:
    query_lower = query.lower()
    for user in users:
        fields = [
            user.get("name", ""),
            user.get("real_name", ""),
            user.get("display_name", ""),
        ]
        if any(query_lower in f.lower() for f in fields):
            return user
    return None


def _find_user_matches(users: list[dict], query: str) -> list[dict]:
    """Find all users matching a query substring. Returns all matches."""
    query_lower = query.lower()
    matches = []
    for user in users:
        fields = [
            user.get("name", ""),
            user.get("real_name", ""),
            user.get("display_name", ""),
        ]
        if any(query_lower in f.lower() for f in fields if f):
            matches.append(user)
    return matches


async def _resolve_mention(client: httpx.AsyncClient, headers: dict, query: str) -> str:
    """Resolve a @(name) mention to a Slack user ID. Errors on ambiguity."""
    # Try cache first, fetch only if no cache exists
    users = _load_cache("users")
    if not users:
        users = await _fetch_all_users(client, headers)

    matches = _find_user_matches(users, query)

    if not matches:
        print(f"User not found for mention: @({query})", file=sys.stderr)
        sys.exit(2)

    if len(matches) == 1:
        return f"<@{matches[0]['id']}>"

    # Multiple matches — check for an exact full-name match
    query_lower = query.lower()
    exact = [u for u in matches if
             (u.get("real_name", "") or "").lower() == query_lower or
             (u.get("display_name", "") or "").lower() == query_lower or
             (u.get("name", "") or "").lower() == query_lower]
    if len(exact) == 1:
        return f"<@{exact[0]['id']}>"

    names = [u.get("real_name") or u.get("display_name") or u.get("name") for u in matches]
    print(f"Ambiguous mention @({query}) — matches: {', '.join(names)}. Be more specific.", file=sys.stderr)
    sys.exit(2)


async def _linkify_mentions(client: httpx.AsyncClient, headers: dict, text: str) -> str:
    """Replace @(name) patterns with Slack <@USER_ID> mentions."""
    mentions = MENTION_RE.findall(text)
    if not mentions:
        return text
    for name in mentions:
        slack_mention = await _resolve_mention(client, headers, name)
        text = text.replace(f"@({name})", slack_mention)
    return text


async def _channel_info(client: httpx.AsyncClient, headers: dict, channel_id: str) -> dict:
    """Look up a single channel by ID — 1 API call instead of paginating all channels."""
    data = await _slack_get(client, headers, "https://slack.com/api/conversations.info", {"channel": channel_id})
    ch = data.get("channel", {})
    return {"id": ch["id"], "name": ch.get("name", channel_id)}


async def _resolve_channel(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    if SLACK_ID_RE.match(query):
        return await _channel_info(client, headers, query)

    # Try cache first
    cached = _load_cache("channels")
    if cached:
        match = _search_channels(cached, query)
        if match:
            return match

    # No cache at all — do initial fetch
    if not cached:
        fresh = await _fetch_all_channels(client, headers)
        match = _search_channels(fresh, query)
        if match:
            return match

    # Channel not found — give actionable advice
    print(f"Channel not found: {query}", file=sys.stderr)
    print("Hint: use --channel-id <ID> to skip name lookup, or run 'channels --refresh' to rebuild the cache.", file=sys.stderr)
    sys.exit(2)


async def _resolve_user(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    if SLACK_ID_RE.match(query):
        return {"id": query, "name": query}

    # Try cache first
    cached = _load_cache("users")
    if cached:
        match = _search_users(cached, query)
        if match:
            return match

    # No cache at all — do initial fetch
    if not cached:
        fresh = await _fetch_all_users(client, headers)
        match = _search_users(fresh, query)
        if match:
            return match

    print(f"User not found: {query}", file=sys.stderr)
    print("Hint: use --user-id <ID> to skip name lookup, or run 'users --refresh' to rebuild the cache.", file=sys.stderr)
    sys.exit(2)


async def _open_dm(client: httpx.AsyncClient, headers: dict, user_id: str) -> str:
    """Open a DM conversation and return the channel ID."""
    data = await _slack_post(client, headers, "https://slack.com/api/conversations.open", {"users": user_id})
    return data["channel"]["id"]


# --- Text resolution ---


def _resolve_text(args: argparse.Namespace) -> str:
    """Resolve message text from --text, --file, or --stdin."""
    if getattr(args, "file", None):
        return Path(args.file).read_text().strip()
    if getattr(args, "stdin", False):
        return sys.stdin.read().strip()
    if getattr(args, "text", None):
        return args.text
    print("One of --text, --file, or --stdin is required", file=sys.stderr)
    sys.exit(1)


# --- Subcommands ---


async def _cmd_send(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        if args.channel_id:
            channel = await _channel_info(client, headers, args.channel_id)
        else:
            channel = await _resolve_channel(client, headers, args.channel)
        text = _linkify_jira_keys(_resolve_text(args))
        text = await _linkify_mentions(client, headers, text)
        payload = {"channel": channel["id"], "text": text}
        if args.thread:
            payload["thread_ts"] = args.thread
        if args.blocks:
            payload["blocks"] = json.loads(Path(args.blocks).read_text())
        data = await _slack_post(client, headers, "https://slack.com/api/chat.postMessage", payload)
        print(json.dumps({
            "ok": True,
            "channel": channel.get("name", channel["id"]),
            "ts": data.get("ts"),
        }))


async def _cmd_dm(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        if args.user_id:
            user = {"id": args.user_id, "name": args.user_id}
        else:
            user = await _resolve_user(client, headers, args.user)
        dm_channel_id = await _open_dm(client, headers, user["id"])
        text = _linkify_jira_keys(_resolve_text(args))
        text = await _linkify_mentions(client, headers, text)
        payload = {"channel": dm_channel_id, "text": text}
        if args.thread:
            payload["thread_ts"] = args.thread
        if args.blocks:
            payload["blocks"] = json.loads(Path(args.blocks).read_text())
        data = await _slack_post(client, headers, "https://slack.com/api/chat.postMessage", payload)
        print(json.dumps({
            "ok": True,
            "user": user.get("real_name", user.get("name", user["id"])),
            "ts": data.get("ts"),
        }))


async def _cmd_channels(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None

    # Use cache if available and filtering; always refresh on --refresh
    if args.refresh:
        _invalidate_cache("channels")

    cached = _load_cache("channels")
    if cached and not args.refresh:
        results = cached
    else:
        async with httpx.AsyncClient() as client:
            results = await _fetch_all_channels(client, headers)

    if filter_lower:
        results = [ch for ch in results if filter_lower in ch.get("name", "").lower()]
    print(json.dumps(results))


async def _cmd_users(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None

    if args.refresh:
        _invalidate_cache("users")

    cached = _load_cache("users")
    if cached and not args.refresh:
        results = cached
    else:
        async with httpx.AsyncClient() as client:
            results = await _fetch_all_users(client, headers)

    if filter_lower:
        results = [u for u in results if any(
            filter_lower in (u.get(f, "") or "").lower()
            for f in ("name", "real_name", "display_name")
        )]
    print(json.dumps(results))


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(description="Send Slack messages to channels and DMs.")
    subparsers = parser.add_subparsers(dest="command")

    send_parser = subparsers.add_parser("send", help="Post a message to a channel")
    send_ch = send_parser.add_mutually_exclusive_group(required=True)
    send_ch.add_argument("--channel", help="Channel name (resolved via lookup)")
    send_ch.add_argument("--channel-id", help="Channel ID (skips name resolution)")
    send_text = send_parser.add_mutually_exclusive_group()
    send_text.add_argument("--text", help="Message text")
    send_text.add_argument("--file", help="Read message text from a file")
    send_text.add_argument("--stdin", action="store_true", help="Read message text from stdin")
    send_parser.add_argument("--thread", default=None, help="Thread timestamp to reply to")
    send_parser.add_argument("--blocks", default=None, help="Path to Block Kit JSON file")

    dm_parser = subparsers.add_parser("dm", help="Send a direct message to a user")
    dm_user = dm_parser.add_mutually_exclusive_group(required=True)
    dm_user.add_argument("--user", help="Username or display name (resolved via lookup)")
    dm_user.add_argument("--user-id", help="User ID (skips name resolution)")
    dm_text = dm_parser.add_mutually_exclusive_group()
    dm_text.add_argument("--text", help="Message text")
    dm_text.add_argument("--file", help="Read message text from a file")
    dm_text.add_argument("--stdin", action="store_true", help="Read message text from stdin")
    dm_parser.add_argument("--thread", default=None, help="Thread timestamp to reply to")
    dm_parser.add_argument("--blocks", default=None, help="Path to Block Kit JSON file")

    channels_parser = subparsers.add_parser("channels", help="List channels")
    channels_parser.add_argument("--filter", default=None, help="Substring filter on channel name")
    channels_parser.add_argument("--refresh", action="store_true", help="Force refresh from Slack API")

    users_parser = subparsers.add_parser("users", help="List/search users")
    users_parser.add_argument("--filter", default=None, help="Substring filter on name/display_name")
    users_parser.add_argument("--refresh", action="store_true", help="Force refresh from Slack API")

    args = parser.parse_args()

    match args.command:
        case "send":
            asyncio.run(_cmd_send(args))
        case "dm":
            asyncio.run(_cmd_dm(args))
        case "channels":
            asyncio.run(_cmd_channels(args))
        case "users":
            asyncio.run(_cmd_users(args))
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
