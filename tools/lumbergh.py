#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: lumbergh
description: Manage Lumbergh session todos, scratchpad, and prompts via the local API
categories: [lumbergh, session, todos, scratchpad, prompts]
usage: |
  sessions list
  [--session NAME] todos list
  [--session NAME] todos add "text" [--description "desc"]
  [--session NAME] todos done <index>
  [--session NAME] todos undone <index>
  [--session NAME] todos remove <index>
  [--session NAME] todos move <index> <target_session>
  [--session NAME] scratchpad get
  [--session NAME] scratchpad set "content"
  [--session NAME] scratchpad append "content"
  [--session NAME] prompts list
  [--session NAME] prompts get <name_or_id>
  [--session NAME] prompts set <name_or_id> <text>
"""

import argparse
import json
import os
import sys
from difflib import SequenceMatcher

import httpx

BASE_URL = "http://localhost:8420"


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=10)


def _request(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        resp = getattr(client, method)(path, **kwargs)
    except httpx.ConnectError:
        print("Lumbergh API not reachable at " + BASE_URL, file=sys.stderr)
        sys.exit(2)
    if resp.status_code >= 400:
        print(f"API error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(2)
    return resp


def _detect_session(client: httpx.Client) -> str:
    cwd = os.path.realpath(os.environ.get("SHERPA_CALLER_CWD", "") or os.getcwd())
    resp = _request(client, "get", "/api/sessions")
    sessions = resp.json().get("sessions", [])

    best_name = None
    best_len = -1
    for session in sessions:
        workdir = session.get("workdir", "")
        if not workdir:
            continue
        workdir = os.path.realpath(workdir)
        if cwd == workdir or cwd.startswith(workdir + "/"):
            if len(workdir) > best_len:
                best_len = len(workdir)
                best_name = session.get("name")

    if not best_name:
        print("Could not auto-detect session from CWD. Use --session.", file=sys.stderr)
        sys.exit(1)
    print(f"Session: {best_name}", file=sys.stderr)
    return best_name


def _get_session_names(client: httpx.Client) -> list[str]:
    resp = _request(client, "get", "/api/sessions")
    sessions = resp.json().get("sessions", [])
    return [s.get("name") for s in sessions if s.get("name")]


def _fuzzy_match(name: str, candidates: list[str], threshold: float = 0.6) -> str | None:
    """Return the best fuzzy match for name among candidates, or None."""
    # Check substring match first (e.g. 'sherpa' in 'project-sherpa')
    sub_matches = [c for c in candidates if name.lower() in c.lower()]
    if len(sub_matches) == 1:
        return sub_matches[0]

    # Fall back to sequence similarity
    best, best_score = None, 0.0
    for c in candidates:
        score = SequenceMatcher(None, name.lower(), c.lower()).ratio()
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= threshold else None


def resolve_session(args: argparse.Namespace, client: httpx.Client) -> str:
    if args.session:
        names = _get_session_names(client)
        if args.session in names:
            return args.session
        # Try fuzzy match
        suggestion = _fuzzy_match(args.session, names)
        if suggestion:
            print(f"Session '{args.session}' not found. Using closest match: '{suggestion}'", file=sys.stderr)
            return suggestion
        print(f"Session '{args.session}' not found. Available sessions:", file=sys.stderr)
        for n in names:
            print(f"  - {n}", file=sys.stderr)
        sys.exit(1)
    return _detect_session(client)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def cmd_sessions_list(client: httpx.Client, _args: argparse.Namespace) -> None:
    resp = _request(client, "get", "/api/sessions")
    sessions = resp.json().get("sessions", [])
    for s in sessions:
        name = s.get("name", "?")
        workdir = s.get("workdir", "")
        print(f"  - {name}  ({workdir})", file=sys.stderr)
    print(json.dumps(sessions))


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

def cmd_todos_list(session: str, client: httpx.Client, _args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/todos")
    todos = resp.json().get("todos", [])
    for i, todo in enumerate(todos, 1):
        status = "x" if todo.get("done") else " "
        print(f"  {i}. [{status}] {todo.get('text', '')}", file=sys.stderr)
    print(json.dumps(todos))


def cmd_todos_add(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/todos")
    todos = resp.json().get("todos", [])
    new_todo = {"text": args.text, "done": False}
    if args.description:
        new_todo["description"] = args.description
    # Insert before completed todos so the UI sections stay correct
    insert_at = next((i for i, t in enumerate(todos) if t.get("done")), len(todos))
    todos.insert(insert_at, new_todo)
    _request(client, "post", f"/api/sessions/{session}/todos", json={"todos": todos})
    print(f"Added todo #{len(todos)}", file=sys.stderr)
    print(json.dumps(new_todo))


def cmd_todos_done(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    _toggle_todo(session, client, args.index, done=True)


def cmd_todos_undone(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    _toggle_todo(session, client, args.index, done=False)


def _toggle_todo(session: str, client: httpx.Client, index: int, done: bool) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/todos")
    todos = resp.json().get("todos", [])
    idx = index - 1
    if idx < 0 or idx >= len(todos):
        print(f"Index {index} out of range (1-{len(todos)})", file=sys.stderr)
        sys.exit(1)
    todos[idx]["done"] = done
    _request(client, "post", f"/api/sessions/{session}/todos", json={"todos": todos})
    label = "done" if done else "undone"
    print(f"Marked todo #{index} as {label}", file=sys.stderr)
    print(json.dumps(todos[idx]))


def cmd_todos_remove(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/todos")
    todos = resp.json().get("todos", [])
    idx = args.index - 1
    if idx < 0 or idx >= len(todos):
        print(f"Index {args.index} out of range (1-{len(todos)})", file=sys.stderr)
        sys.exit(1)
    removed = todos.pop(idx)
    _request(client, "post", f"/api/sessions/{session}/todos", json={"todos": todos})
    print(f"Removed todo #{args.index}", file=sys.stderr)
    print(json.dumps(removed))


def cmd_todos_move(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    idx = args.index - 1
    body = {"todo_index": idx, "target_session": args.target_session}
    resp = _request(client, "post", f"/api/sessions/{session}/todos/move", json=body)
    print(f"Moved todo #{args.index} to {args.target_session}", file=sys.stderr)
    print(json.dumps(resp.json()))


# ---------------------------------------------------------------------------
# Scratchpad
# ---------------------------------------------------------------------------

def cmd_scratchpad_get(session: str, client: httpx.Client, _args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/scratchpad")
    data = resp.json()
    print(json.dumps(data))


def cmd_scratchpad_set(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    _request(client, "post", f"/api/sessions/{session}/scratchpad", json={"content": args.content})
    print("Scratchpad updated", file=sys.stderr)
    print(json.dumps({"content": args.content}))


def cmd_scratchpad_append(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/scratchpad")
    existing = resp.json().get("content", "")
    new_content = existing + "\n" + args.content if existing else args.content
    _request(client, "post", f"/api/sessions/{session}/scratchpad", json={"content": new_content})
    print("Scratchpad appended", file=sys.stderr)
    print(json.dumps({"content": new_content}))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def cmd_prompts_list(session: str, client: httpx.Client, _args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/prompts")
    prompts = resp.json().get("templates", [])
    for p in prompts:
        print(f"  - {p.get('name', p.get('id', '?'))}", file=sys.stderr)
    print(json.dumps(prompts))


def _find_prompt(prompts: list, name_or_id: str) -> tuple[int, dict] | None:
    for i, p in enumerate(prompts):
        if p.get("name") == name_or_id or str(p.get("id")) == name_or_id:
            return i, p
    return None


def cmd_prompts_get(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/prompts")
    prompts = resp.json().get("templates", [])
    result = _find_prompt(prompts, args.name_or_id)
    if not result:
        available = [p.get("name", p.get("id", "?")) for p in prompts]
        print(f"Prompt '{args.name_or_id}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result[1]))


def cmd_prompts_set(session: str, client: httpx.Client, args: argparse.Namespace) -> None:
    resp = _request(client, "get", f"/api/sessions/{session}/prompts")
    prompts = resp.json().get("templates", [])
    result = _find_prompt(prompts, args.name_or_id)
    if not result:
        available = [p.get("name", p.get("id", "?")) for p in prompts]
        print(f"Prompt '{args.name_or_id}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)
    idx, _ = result
    prompts[idx]["prompt"] = args.text
    _request(client, "post", f"/api/sessions/{session}/prompts", json={"templates": prompts})
    print(f"Updated prompt '{args.name_or_id}'", file=sys.stderr)
    print(json.dumps(prompts[idx]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manage Lumbergh session todos, scratchpad, and prompts.",
    )
    parser.add_argument("--session", default=None, help="Session name (default: auto-detect from CWD)")

    resource_sub = parser.add_subparsers(dest="resource")

    # --- sessions ---
    sessions_parser = resource_sub.add_parser("sessions", help="Manage sessions")
    sessions_sub = sessions_parser.add_subparsers(dest="action")
    sessions_sub.add_parser("list", help="List all sessions")

    # --- todos ---
    todos_parser = resource_sub.add_parser("todos", help="Manage session todos")
    todos_sub = todos_parser.add_subparsers(dest="action")

    todos_sub.add_parser("list", help="List all todos")

    p = todos_sub.add_parser("add", help="Add a new todo")
    p.add_argument("text", help="Todo text")
    p.add_argument("--description", default=None, help="Optional description")

    p = todos_sub.add_parser("done", help="Mark a todo as done")
    p.add_argument("index", type=int, help="1-based todo index")

    p = todos_sub.add_parser("undone", help="Mark a todo as not done")
    p.add_argument("index", type=int, help="1-based todo index")

    p = todos_sub.add_parser("remove", help="Remove a todo")
    p.add_argument("index", type=int, help="1-based todo index")

    p = todos_sub.add_parser("move", help="Move a todo to another session")
    p.add_argument("index", type=int, help="1-based todo index")
    p.add_argument("target_session", help="Target session name")

    # --- scratchpad ---
    scratch_parser = resource_sub.add_parser("scratchpad", help="Manage session scratchpad")
    scratch_sub = scratch_parser.add_subparsers(dest="action")

    scratch_sub.add_parser("get", help="Get scratchpad content")

    p = scratch_sub.add_parser("set", help="Set scratchpad content")
    p.add_argument("content", help="New scratchpad content")

    p = scratch_sub.add_parser("append", help="Append to scratchpad")
    p.add_argument("content", help="Content to append")

    # --- prompts ---
    prompts_parser = resource_sub.add_parser("prompts", help="Manage session prompts")
    prompts_sub = prompts_parser.add_subparsers(dest="action")

    prompts_sub.add_parser("list", help="List all prompts")

    p = prompts_sub.add_parser("get", help="Get a prompt by name or ID")
    p.add_argument("name_or_id", help="Prompt name or ID")

    p = prompts_sub.add_parser("set", help="Update a prompt's text")
    p.add_argument("name_or_id", help="Prompt name or ID")
    p.add_argument("text", help="New prompt text")

    args = parser.parse_args()

    if not args.resource:
        parser.print_help()
        sys.exit(1)

    with _client() as client:
        # Sessions commands don't need a resolved session
        if args.resource == "sessions":
            match args.action:
                case "list":
                    cmd_sessions_list(client, args)
                case _:
                    sessions_parser.print_help()
                    sys.exit(1)
            return

        session = resolve_session(args, client)

        match (args.resource, args.action):
            case ("todos", "list"):
                cmd_todos_list(session, client, args)
            case ("todos", "add"):
                cmd_todos_add(session, client, args)
            case ("todos", "done"):
                cmd_todos_done(session, client, args)
            case ("todos", "undone"):
                cmd_todos_undone(session, client, args)
            case ("todos", "remove"):
                cmd_todos_remove(session, client, args)
            case ("todos", "move"):
                cmd_todos_move(session, client, args)
            case ("scratchpad", "get"):
                cmd_scratchpad_get(session, client, args)
            case ("scratchpad", "set"):
                cmd_scratchpad_set(session, client, args)
            case ("scratchpad", "append"):
                cmd_scratchpad_append(session, client, args)
            case ("prompts", "list"):
                cmd_prompts_list(session, client, args)
            case ("prompts", "get"):
                cmd_prompts_get(session, client, args)
            case ("prompts", "set"):
                cmd_prompts_set(session, client, args)
            case (resource, None):
                # No action given — print help for the resource subparser
                {"todos": todos_parser, "scratchpad": scratch_parser, "prompts": prompts_parser, "sessions": sessions_parser}[resource].print_help()
                sys.exit(1)
            case _:
                parser.print_help()
                sys.exit(1)


if __name__ == "__main__":
    main()
