#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["ddgs"]
# ///
"""
name: web_search
description: Search the web using DuckDuckGo and return results as JSON
categories: [web, search, research]
usage: |
  search 'your query here' [--limit 5]
"""

import argparse
import json
import sys

from ddgs import DDGS


def cmd_search(args: argparse.Namespace) -> None:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(args.query, max_results=args.limit))
    except Exception as e:
        print(f"Search failed: {e}", file=sys.stderr)
        sys.exit(2)

    output = []
    for r in results:
        output.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        })

    print(json.dumps({"query": args.query, "results": output}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Search the web via DuckDuckGo.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="Search the web")
    p.add_argument("query", help="Search query")
    p.add_argument("--limit", type=int, default=5, help="Number of results (default: 5)")

    args = parser.parse_args()

    match args.command:
        case "search":
            cmd_search(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
