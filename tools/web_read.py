#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: web_read
description: Fetch a URL and return clean readable markdown via Jina Reader
categories: [web, read, fetch, research]
usage: |
  fetch 'https://example.com/page'
"""

import argparse
import json
import sys

import httpx


JINA_READER_PREFIX = "https://r.jina.ai/"


def cmd_fetch(args: argparse.Namespace) -> None:
    url = f"{JINA_READER_PREFIX}{args.url}"
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers={"Accept": "text/markdown"})
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        sys.exit(2)

    if resp.status_code != 200:
        print(f"Fetch failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        sys.exit(2)

    content = resp.text
    print(json.dumps({"url": args.url, "content": content}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Fetch a URL as clean markdown via Jina Reader.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("fetch", help="Fetch a URL")
    p.add_argument("url", help="URL to fetch")

    args = parser.parse_args()

    match args.command:
        case "fetch":
            cmd_fetch(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
