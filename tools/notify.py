#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: notify
description: Send a push notification to your phone via ntfy.sh. Supports title, priority, tags/emoji, and click URL.
categories: [notification, push, ntfy, alerts]
secrets:
  - NTFY_TOPIC
usage: |
  notify "<message>" [--title '<title>'] [--priority {min,low,default,high,urgent}]
         [--tags tag1,tag2] [--click <url>] [--topic <topic-override>]
  Vault keys:
    NTFY_TOPIC    (required)   — your private ntfy topic name
    NTFY_SERVER   (optional)   — base URL, defaults to https://ntfy.sh
    NTFY_TOKEN    (optional)   — bearer token for auth-protected topics
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"

_PRIORITY_MAP = {"min": "1", "low": "2", "default": "3", "high": "4", "urgent": "5"}


def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _required(vault: dict, key: str) -> str:
    val = vault.get(key)
    if not val:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a push notification via ntfy.sh.")
    parser.add_argument("message", help="Notification body")
    parser.add_argument("--title", default=None, help="Notification title")
    parser.add_argument(
        "--priority",
        choices=list(_PRIORITY_MAP),
        default="default",
        help="Notification priority (default: default)",
    )
    parser.add_argument("--tags", default=None, help="Comma-separated tags / emoji shortcodes (e.g. warning,skull)")
    parser.add_argument("--click", default=None, help="URL to open when notification is tapped")
    parser.add_argument("--topic", default=None, help="Override the default NTFY_TOPIC from vault")
    args = parser.parse_args()

    vault = _load_vault()
    topic = args.topic or _required(vault, "NTFY_TOPIC")
    server = (vault.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    token = vault.get("NTFY_TOKEN")

    headers: dict[str, str] = {}
    if args.title:
        headers["Title"] = args.title
    if args.priority != "default":
        headers["Priority"] = _PRIORITY_MAP[args.priority]
    if args.tags:
        headers["Tags"] = args.tags
    if args.click:
        headers["Click"] = args.click
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{server}/{topic}"
    try:
        resp = httpx.post(url, content=args.message.encode("utf-8"), headers=headers, timeout=10)
    except httpx.HTTPError as e:
        print(f"Failed to reach {url}: {e}", file=sys.stderr)
        sys.exit(2)

    if resp.status_code >= 300:
        print(f"ntfy returned {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(2)

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    print(json.dumps({
        "topic": topic,
        "server": server,
        "id": body.get("id"),
        "time": body.get("time"),
    }))


if __name__ == "__main__":
    main()
