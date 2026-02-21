#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: sentry_issues
description: Fetch and resolve issues from a self-hosted Sentry instance by URL
categories: [sentry, debugging, error-tracking, issues]
secrets:
  - SENTRY_AUTH_TOKEN
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
DEFAULT_BASE_URL = "https://sentry-dev.gravitate.energy"


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _parse_sentry_url(url: str) -> dict:
    """Extract org slug, issue ID, environment, and base URL from a Sentry issue URL."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    match = re.match(r"/organizations/([^/]+)/issues/(\d+)/?", parsed.path)
    if not match:
        print(f"Could not parse Sentry issue URL: {url}", file=sys.stderr)
        sys.exit(1)

    query = parse_qs(parsed.query)

    return {
        "base_url": base_url,
        "org_slug": match.group(1),
        "issue_id": match.group(2),
        "environment": query.get("environment", [None])[0],
    }


def _truncate_stacktrace(frames: list, max_frames: int = 15) -> list:
    """Keep the most recent frames (end of list) and truncate the rest."""
    truncated = []
    for frame in frames[-max_frames:]:
        truncated.append({
            "filename": frame.get("filename"),
            "function": frame.get("function"),
            "lineno": frame.get("lineNo") or frame.get("lineno"),
            "context_line": frame.get("contextLine") or frame.get("context_line"),
        })
    return truncated


def cmd_fetch(args: argparse.Namespace) -> None:
    token = _load_secret("SENTRY_AUTH_TOKEN")
    url_info = _parse_sentry_url(args.url)

    base_url = args.base_url or url_info["base_url"]
    issue_id = url_info["issue_id"]
    environment = url_info["environment"]

    headers = {"Authorization": f"Bearer {token}"}
    params = {}
    if environment:
        params["environment"] = environment

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        # Fetch issue details
        print(f"Fetching issue {issue_id}...", file=sys.stderr)
        resp = client.get(f"/api/0/issues/{issue_id}/", params=params)
        if resp.status_code != 200:
            print(f"Failed to fetch issue: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        issue_data = resp.json()

        # Fetch latest event
        print("Fetching latest event...", file=sys.stderr)
        resp = client.get(f"/api/0/issues/{issue_id}/events/latest/", params=params)
        if resp.status_code != 200:
            print(f"Failed to fetch latest event: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        event_data = resp.json()

    # Build exception frames from event
    exception_values = []
    entries = event_data.get("entries", [])
    for entry in entries:
        if entry.get("type") == "exception":
            for exc_val in entry.get("data", {}).get("values", []):
                frames = exc_val.get("stacktrace", {}).get("frames", [])
                exception_values.append({
                    "type": exc_val.get("type"),
                    "value": exc_val.get("value"),
                    "frames": _truncate_stacktrace(frames),
                })

    # Extract message
    message = event_data.get("message", "")
    if not message:
        for entry in entries:
            if entry.get("type") == "message":
                message = entry.get("data", {}).get("formatted") or entry.get("data", {}).get("message", "")
                break

    output = {
        "issue": {
            "id": issue_data.get("id"),
            "title": issue_data.get("title"),
            "culprit": issue_data.get("culprit"),
            "status": issue_data.get("status"),
            "level": issue_data.get("level"),
            "first_seen": issue_data.get("firstSeen"),
            "last_seen": issue_data.get("lastSeen"),
            "count": issue_data.get("count"),
            "permalink": issue_data.get("permalink"),
            "tags": [
                {"key": t.get("key"), "value": t.get("value")}
                for t in issue_data.get("tags", [])
            ],
        },
        "latest_event": {
            "event_id": event_data.get("eventID"),
            "timestamp": event_data.get("dateCreated"),
            "exception": exception_values,
            "message": message,
        },
    }

    print(json.dumps(output, indent=2))


def cmd_resolve(args: argparse.Namespace) -> None:
    token = _load_secret("SENTRY_AUTH_TOKEN")
    url_info = _parse_sentry_url(args.url)

    base_url = args.base_url or url_info["base_url"]
    issue_id = url_info["issue_id"]

    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        print(f"Resolving issue {issue_id}...", file=sys.stderr)
        resp = client.put(
            f"/api/0/issues/{issue_id}/",
            json={"status": "resolved"},
        )
        if resp.status_code != 200:
            print(f"Failed to resolve issue: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        data = resp.json()

    print(json.dumps({
        "id": data.get("id"),
        "status": data.get("status"),
        "title": data.get("title"),
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Fetch and resolve Sentry issues by URL.")
    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch issue details from a Sentry URL")
    fetch_parser.add_argument("url", help="Full Sentry issue URL")
    fetch_parser.add_argument("--base-url", default=None, help="Override Sentry base URL (default: extracted from URL)")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a Sentry issue by URL")
    resolve_parser.add_argument("url", help="Full Sentry issue URL")
    resolve_parser.add_argument("--base-url", default=None, help="Override Sentry base URL (default: extracted from URL)")

    args = parser.parse_args()

    match args.command:
        case "fetch":
            cmd_fetch(args)
        case "resolve":
            cmd_resolve(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
