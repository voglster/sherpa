#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: knowledge
description: Store and search common facts like repo paths, service URLs, and host details.
categories: [sherpa, knowledge, reference, facts]
usage: |
  add <key> <value> [--tags tag1,tag2] [--description "..."]
  search <query>
  get <key>
  list
  remove <key>
"""

import argparse
import json
import sys
from pathlib import Path

STORE_DIR = Path.home() / ".sherpa"
STORE_PATH = STORE_DIR / "knowledge.json"


def _load_store() -> list[dict]:
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text())
    return []


def _save_store(entries: list[dict]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(entries, indent=2) + "\n")


def _find_index(entries: list[dict], key: str) -> int | None:
    for i, e in enumerate(entries):
        if e["key"] == key:
            return i
    return None


def _score_entry(entry: dict, tokens: list[str]) -> float:
    """Score an entry against query tokens.

    Weights: key=3, tags=2, description=1.
    Exact word matches get a 1.5x bonus.
    """
    key = entry["key"].lower()
    tags = " ".join(entry.get("tags", [])).lower()
    description = entry.get("description", "").lower()
    key_words = set(key.replace("_", " ").replace("-", " ").split())
    tag_words = set(tags.replace("-", " ").split())
    desc_words = set(description.split())

    score = 0.0
    for token in tokens:
        if token in key:
            score += 3.0
            if token in key_words:
                score += 3.0 * 0.5
        if token in tags:
            score += 2.0
            if token in tag_words:
                score += 2.0 * 0.5
        if token in description:
            score += 1.0
            if token in desc_words:
                score += 1.0 * 0.5

    return score


def main():
    parser = argparse.ArgumentParser(
        description="Store and search common facts like repo paths, service URLs, and host details."
    )
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add", help="Add or update an entry")
    add_p.add_argument("key", help="Unique key for the entry")
    add_p.add_argument("value", help="Value to store")
    add_p.add_argument("--tags", help="Comma-separated tags")
    add_p.add_argument("--description", help="Short description")

    search_p = sub.add_parser("search", help="Search entries by keyword")
    search_p.add_argument("query", help="Search query")

    get_p = sub.add_parser("get", help="Get entry by exact key")
    get_p.add_argument("key", help="Entry key")

    sub.add_parser("list", help="List all entries")

    rm_p = sub.add_parser("remove", help="Remove an entry")
    rm_p.add_argument("key", help="Entry key to remove")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "add":
        entries = _load_store()
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        entry = {
            "key": args.key,
            "value": args.value,
            "tags": tags,
            "description": args.description or "",
        }
        idx = _find_index(entries, args.key)
        if idx is not None:
            entries[idx] = entry
            action = "updated"
        else:
            entries.append(entry)
            action = "added"
        _save_store(entries)
        print(json.dumps({"status": "ok", "key": args.key, "action": action}))

    elif args.command == "search":
        entries = _load_store()
        tokens = args.query.lower().split()
        scored = [(e, _score_entry(e, tokens)) for e in entries]
        scored = [(e, s) for e, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        results = [e for e, _ in scored[:5]]
        print(json.dumps({"results": results}))

    elif args.command == "get":
        entries = _load_store()
        idx = _find_index(entries, args.key)
        if idx is None:
            print(f"Key not found: {args.key}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(entries[idx]))

    elif args.command == "list":
        entries = _load_store()
        print(json.dumps({"entries": entries}))

    elif args.command == "remove":
        entries = _load_store()
        idx = _find_index(entries, args.key)
        if idx is None:
            print(f"Key not found: {args.key}", file=sys.stderr)
            sys.exit(1)
        removed = entries.pop(idx)
        _save_store(entries)
        print(json.dumps({"status": "ok", "key": removed["key"], "action": "removed"}))


if __name__ == "__main__":
    main()
