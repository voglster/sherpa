#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: notes_search
description: Search personal notes and explore the knowledge graph via Obsidian CLI.
categories: [notes, search, knowledge]
usage: |
  search --query "how does auth work"
  search --query "deploy process" --limit 10
  search --query "meeting notes" --path "work/"
  search --query "k8s" --context
  context --file "Isaac Labauve"
  context --file "Isaac Labauve" --depth 2 --snippet-chars 500
  links --file "Isaac Labauve"
  backlinks --file "Isaac Labauve"
notes: |
  Typical workflow: search to find relevant notes, then context to explore the graph around them.
  The context command returns the note content, outgoing links, backlinks, and a snippet from each
  linked note — giving a full picture in one call.
"""

import argparse
import json
import subprocess
import sys


SNIPPET_CHARS = 300


def _run_obsidian(*args: str) -> str:
    result = subprocess.run(
        ["obsidian", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"obsidian error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return result.stdout


def _read_note(file: str) -> str:
    return _run_obsidian("read", f"file={file}").strip()


def _get_links(file: str) -> list[str]:
    output = _run_obsidian("links", f"file={file}").strip()
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _get_backlinks(file: str) -> list[str]:
    output = _run_obsidian("backlinks", f"file={file}", "format=json").strip()
    if not output:
        return []
    try:
        entries = json.loads(output)
        return [e["file"] for e in entries if "file" in e]
    except (json.JSONDecodeError, TypeError):
        return [line.strip() for line in output.splitlines() if line.strip()]


def _file_label(path: str) -> str:
    """Strip folder prefix and .md suffix for display."""
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return name


def _snippet(file: str, max_chars: int) -> str | None:
    try:
        content = _run_obsidian("read", f"path={file}").strip()
    except SystemExit:
        return None
    if not content:
        return None
    return content[:max_chars]


def cmd_search(args: argparse.Namespace) -> None:
    cmd = "search:context" if args.context else "search"
    obsidian_args = [cmd, f"query={args.query}", "format=json"]

    if args.limit:
        obsidian_args.append(f"limit={args.limit}")
    if args.path:
        obsidian_args.append(f"path={args.path}")

    output = _run_obsidian(*obsidian_args)

    if not output.strip():
        print(json.dumps({"query": args.query, "results": [], "count": 0}))
        return

    try:
        results = json.loads(output)
    except json.JSONDecodeError:
        print(json.dumps({"query": args.query, "raw": output.strip()}))
        return

    if isinstance(results, list):
        print(json.dumps({"query": args.query, "results": results, "count": len(results)}))
    else:
        print(json.dumps({"query": args.query, "results": results}))


def cmd_context(args: argparse.Namespace) -> None:
    snippet_chars = args.snippet_chars
    content = _read_note(args.file)
    links = _get_links(args.file)
    backlinks = _get_backlinks(args.file)

    # Dedupe: collect all unique linked files
    all_linked = dict.fromkeys(links + backlinks)

    linked_snippets = []
    for path in all_linked:
        snip = _snippet(path, snippet_chars)
        if snip is not None:
            linked_snippets.append({
                "file": path,
                "name": _file_label(path),
                "snippet": snip,
            })

    # Optionally go one level deeper
    if args.depth >= 2:
        second_hop = {}
        for path in all_linked:
            name = _file_label(path)
            hop_links = _get_links(name)
            hop_backlinks = _get_backlinks(name)
            neighbors = list(dict.fromkeys(hop_links + hop_backlinks))
            # Filter out notes we already have
            new = [n for n in neighbors if n not in all_linked and _file_label(n) != args.file]
            if new:
                second_hop[path] = new
                for n in new:
                    snip = _snippet(n, snippet_chars)
                    if snip is not None:
                        linked_snippets.append({
                            "file": n,
                            "name": _file_label(n),
                            "snippet": snip,
                            "via": path,
                        })

    output = {
        "file": args.file,
        "content": content,
        "links": links,
        "backlinks": backlinks,
        "linked_snippets": linked_snippets,
    }
    print(json.dumps(output, indent=2))


def cmd_links(args: argparse.Namespace) -> None:
    links = _get_links(args.file)
    print(json.dumps({"file": args.file, "links": links, "count": len(links)}))


def cmd_backlinks(args: argparse.Namespace) -> None:
    backlinks = _get_backlinks(args.file)
    print(json.dumps({"file": args.file, "backlinks": backlinks, "count": len(backlinks)}))


def main():
    parser = argparse.ArgumentParser(
        description="Search personal notes and explore the knowledge graph via Obsidian CLI."
    )
    sub = parser.add_subparsers(dest="command")

    search_p = sub.add_parser("search", help="Search notes")
    search_p.add_argument("--query", required=True, help="Search query")
    search_p.add_argument("--limit", type=int, default=None, help="Max results")
    search_p.add_argument("--path", default=None, help="Limit to folder path")
    search_p.add_argument("--context", action="store_true", help="Include matching line context")

    ctx_p = sub.add_parser("context", help="Get a note with its graph neighborhood")
    ctx_p.add_argument("--file", required=True, help="Note name (wiki-link style, no .md)")
    ctx_p.add_argument("--depth", type=int, default=1, choices=[1, 2], help="Graph traversal depth (default: 1)")
    ctx_p.add_argument("--snippet-chars", type=int, default=SNIPPET_CHARS, help=f"Max chars per linked snippet (default: {SNIPPET_CHARS})")

    links_p = sub.add_parser("links", help="List outgoing links from a note")
    links_p.add_argument("--file", required=True, help="Note name")

    bl_p = sub.add_parser("backlinks", help="List notes that link to this note")
    bl_p.add_argument("--file", required=True, help="Note name")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "search": cmd_search,
        "context": cmd_context,
        "links": cmd_links,
        "backlinks": cmd_backlinks,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
