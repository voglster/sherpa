#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: slack_messenger
description: Send Slack messages to channels and DMs, with fuzzy user/channel lookup.
categories: [slack, messaging, communication]
secrets:
  - SLACK_USER_TOKEN
usage: |
  send --channel <name/id> --text 'Hello team!'
  dm --user <name/id> --text 'Hey, quick question...'
  channels [--filter general]
  users [--filter swap]
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
SLACK_ID_RE = re.compile(r"^[CGUWDBT][A-Z0-9]{8,}$")


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


# --- Slack API helpers ---


async def _slack_get(client: httpx.AsyncClient, headers: dict, url: str, params: dict | None = None) -> dict:
    resp = await client.get(url, headers=headers, params=params or {})
    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        print(f"Slack API error: {error}", file=sys.stderr)
        sys.exit(2)
    return data


async def _slack_post(client: httpx.AsyncClient, headers: dict, url: str, payload: dict) -> dict:
    resp = await client.post(url, headers=headers, json=payload)
    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        print(f"Slack API error: {error}", file=sys.stderr)
        sys.exit(2)
    return data


async def _resolve_channel(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    """Resolve a channel name or ID to a channel object."""
    if SLACK_ID_RE.match(query):
        return {"id": query, "name": query}

    query_lower = query.lstrip("#").lower()
    cursor = None
    while True:
        params = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/conversations.list", params)
        for ch in data.get("channels", []):
            if query_lower in ch.get("name", "").lower():
                return ch
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"Channel not found: {query}", file=sys.stderr)
    sys.exit(2)


async def _resolve_user(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    """Resolve a user by name/display_name/id with fuzzy substring match."""
    if SLACK_ID_RE.match(query):
        return {"id": query, "name": query}

    query_lower = query.lower()
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/users.list", params)
        for user in data.get("members", []):
            if user.get("deleted") or user.get("is_bot"):
                continue
            fields = [
                user.get("name", ""),
                user.get("real_name", ""),
                user.get("profile", {}).get("display_name", ""),
            ]
            if any(query_lower in f.lower() for f in fields):
                return user
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    print(f"User not found: {query}", file=sys.stderr)
    sys.exit(2)


async def _open_dm(client: httpx.AsyncClient, headers: dict, user_id: str) -> str:
    """Open a DM conversation and return the channel ID."""
    data = await _slack_post(client, headers, "https://slack.com/api/conversations.open", {"users": user_id})
    return data["channel"]["id"]


# --- Subcommands ---


async def _cmd_send(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        channel = await _resolve_channel(client, headers, args.channel)
        data = await _slack_post(client, headers, "https://slack.com/api/chat.postMessage", {
            "channel": channel["id"],
            "text": args.text,
        })
        print(json.dumps({
            "ok": True,
            "channel": channel.get("name", channel["id"]),
            "ts": data.get("ts"),
        }))


async def _cmd_dm(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        user = await _resolve_user(client, headers, args.user)
        dm_channel_id = await _open_dm(client, headers, user["id"])
        data = await _slack_post(client, headers, "https://slack.com/api/chat.postMessage", {
            "channel": dm_channel_id,
            "text": args.text,
        })
        print(json.dumps({
            "ok": True,
            "user": user.get("real_name", user.get("name", user["id"])),
            "ts": data.get("ts"),
        }))


async def _cmd_channels(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None
    results = []
    async with httpx.AsyncClient() as client:
        cursor = None
        while True:
            params = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await _slack_get(client, headers, "https://slack.com/api/conversations.list", params)
            for ch in data.get("channels", []):
                if filter_lower and filter_lower not in ch.get("name", "").lower():
                    continue
                results.append({
                    "id": ch["id"],
                    "name": ch.get("name"),
                    "num_members": ch.get("num_members", 0),
                })
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    print(json.dumps(results))


async def _cmd_users(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None
    results = []
    async with httpx.AsyncClient() as client:
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await _slack_get(client, headers, "https://slack.com/api/users.list", params)
            for user in data.get("members", []):
                if user.get("deleted") or user.get("is_bot"):
                    continue
                fields = [
                    user.get("name", ""),
                    user.get("real_name", ""),
                    user.get("profile", {}).get("display_name", ""),
                ]
                if filter_lower and not any(filter_lower in f.lower() for f in fields):
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
    print(json.dumps(results))


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(description="Send Slack messages to channels and DMs.")
    subparsers = parser.add_subparsers(dest="command")

    send_parser = subparsers.add_parser("send", help="Post a message to a channel")
    send_parser.add_argument("--channel", required=True, help="Channel name or ID")
    send_parser.add_argument("--text", required=True, help="Message text")

    dm_parser = subparsers.add_parser("dm", help="Send a direct message to a user")
    dm_parser.add_argument("--user", required=True, help="Username, display name, or user ID")
    dm_parser.add_argument("--text", required=True, help="Message text")

    channels_parser = subparsers.add_parser("channels", help="List channels")
    channels_parser.add_argument("--filter", default=None, help="Substring filter on channel name")

    users_parser = subparsers.add_parser("users", help="List/search users")
    users_parser.add_argument("--filter", default=None, help="Substring filter on name/display_name")

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
