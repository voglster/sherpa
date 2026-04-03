#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: notes_search
description: Search, read, create, and edit personal notes via Obsidian CLI. Explore the knowledge graph with links/backlinks and context traversal.
categories: [notes, search, knowledge]
usage: |
  search --query "how does auth work"
  search --query "deploy process" --limit 10
  search --query "k8s" --context
  context --file "Isaac Labauve"
  context --file "Isaac Labauve" --depth 2
  read --file "Isaac Labauve"
  create --name "Meeting Notes 2026-04-03" --content "## Attendees ..."
  create --name "New Idea" --path "Slipbox/New Idea.md" --content "some content"
  append --file "Isaac Labauve" --content "## Update ..."
  edit --file "Isaac Labauve" --content "Full replacement content"
  rename --file "Old Name" --name "New Name"
  delete --file "Scratch Note"
  tags
  tags --file "Isaac Labauve"
  links --file "Isaac Labauve"
  backlinks --file "Isaac Labauve"
notes: |
  Typical workflow: search to find relevant notes, then context to explore the graph around them.
  The context command returns the note content, outgoing links, backlinks, and a snippet from each
  linked note — giving a full picture in one call.
  Use read to get full note content. Use create/append/edit for modifications.
  Obsidian must be running. The CLI connects via /run/user/<uid>/.obsidian-cli.sock.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


SNIPPET_CHARS = 300
VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _obsidian_env() -> dict[str, str]:
    """Build an env dict that ensures the obsidian CLI can reach the running app.

    The MCP subprocess may lack DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR.
    Pull DBUS_SESSION_BUS_ADDRESS from vault (OBSIDIAN_DBUS_ADDRESS) if not
    already in the environment.
    """
    env = os.environ.copy()
    # The obsidian CLI connects via /run/user/<uid>/.obsidian-cli.sock
    # which it locates using XDG_RUNTIME_DIR. MCP subprocesses may lack this.
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


def _run_obsidian(*args: str) -> str:
    env = _obsidian_env()
    try:
        result = subprocess.run(
            ["obsidian", *args],
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        print(json.dumps({"error": "obsidian CLI not found in PATH", "PATH": env.get("PATH", "")}))
        sys.exit(2)
    if result.returncode != 0:
        # Surface the error in stdout JSON so the agent loop can see it
        print(json.dumps({
            "error": f"obsidian exited {result.returncode}",
            "stderr": result.stderr.strip(),
            "stdout": result.stdout.strip(),
            "args": list(args),
            "env_keys": sorted(env.keys()),
            "has_dbus": "DBUS_SESSION_BUS_ADDRESS" in env,
            "dbus_value": env.get("DBUS_SESSION_BUS_ADDRESS", ""),
        }))
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


def cmd_read(args: argparse.Namespace) -> None:
    if args.path:
        content = _run_obsidian("read", f"path={args.path}").strip()
    else:
        content = _read_note(args.file)
    print(json.dumps({"file": args.file or args.path, "content": content}))


def cmd_create(args: argparse.Namespace) -> None:
    obsidian_args = ["create", f"name={args.name}"]
    if args.path:
        obsidian_args.append(f"path={args.path}")
    if args.content:
        obsidian_args.append(f"content={args.content}")
    output = _run_obsidian(*obsidian_args)
    print(json.dumps({"status": "created", "name": args.name, "output": output.strip()}))


def cmd_append(args: argparse.Namespace) -> None:
    _run_obsidian("append", f"file={args.file}", f"content={args.content}")
    print(json.dumps({"status": "appended", "file": args.file}))


def cmd_edit(args: argparse.Namespace) -> None:
    """Replace full note content (create --overwrite)."""
    obsidian_args = ["create", f"name={args.file}", f"content={args.content}", "overwrite"]
    if args.path:
        obsidian_args = ["create", f"path={args.path}", f"content={args.content}", "overwrite"]
    _run_obsidian(*obsidian_args)
    print(json.dumps({"status": "updated", "file": args.file or args.path}))


def cmd_rename(args: argparse.Namespace) -> None:
    _run_obsidian("rename", f"file={args.file}", f"name={args.name}")
    print(json.dumps({"status": "renamed", "from": args.file, "to": args.name}))


def cmd_delete(args: argparse.Namespace) -> None:
    _run_obsidian("delete", f"file={args.file}")
    print(json.dumps({"status": "deleted", "file": args.file}))


def cmd_tags(args: argparse.Namespace) -> None:
    obsidian_args = ["tags", "format=json", "counts"]
    if args.file:
        obsidian_args.append(f"file={args.file}")
    output = _run_obsidian(*obsidian_args).strip()
    try:
        tags = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        tags = []
    if isinstance(tags, list):
        print(json.dumps({"tags": tags, "count": len(tags)}))
    else:
        print(json.dumps({"tags": tags}))


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

    read_p = sub.add_parser("read", help="Read full note content")
    read_p.add_argument("--file", default=None, help="Note name (wiki-link style)")
    read_p.add_argument("--path", default=None, help="Exact file path")

    create_p = sub.add_parser("create", help="Create a new note")
    create_p.add_argument("--name", required=True, help="Note name")
    create_p.add_argument("--path", default=None, help="Exact file path (e.g. Slipbox/Note.md)")
    create_p.add_argument("--content", default=None, help="Initial content")

    append_p = sub.add_parser("append", help="Append content to a note")
    append_p.add_argument("--file", required=True, help="Note name")
    append_p.add_argument("--content", required=True, help="Content to append")

    edit_p = sub.add_parser("edit", help="Replace full note content")
    edit_p.add_argument("--file", default=None, help="Note name")
    edit_p.add_argument("--path", default=None, help="Exact file path")
    edit_p.add_argument("--content", required=True, help="New content")

    rename_p = sub.add_parser("rename", help="Rename a note")
    rename_p.add_argument("--file", required=True, help="Current note name")
    rename_p.add_argument("--name", required=True, help="New name")

    delete_p = sub.add_parser("delete", help="Delete a note (moves to trash)")
    delete_p.add_argument("--file", required=True, help="Note name")

    tags_p = sub.add_parser("tags", help="List tags (vault-wide or per note)")
    tags_p.add_argument("--file", default=None, help="Note name (omit for vault-wide)")

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
        "read": cmd_read,
        "create": cmd_create,
        "append": cmd_append,
        "edit": cmd_edit,
        "rename": cmd_rename,
        "delete": cmd_delete,
        "tags": cmd_tags,
        "links": cmd_links,
        "backlinks": cmd_backlinks,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
