#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: jira_admin
description: Advanced Jira operations - epic assignment, subtask management, issue linking, and workflow automation
categories: [jira, admin, project-management, workflow, subtasks, epic, linking]
secrets:
  - JIRA_URL
  - JIRA_USERNAME
  - JIRA_API_TOKEN
usage: |
  epic <ISSUE_KEY> --epic <EPIC_KEY>
  subtask <PARENT_KEY> --type dev|validate|bug [--assignee name] [--summary '...']
  complete-subtask <PARENT_KEY> --type dev|validate
  assign-subtask <PARENT_KEY> --type dev|validate --assignee <name>
  users <query>           Search Jira users and cache the match
  users <query> --select N  Pick result N from ambiguous search to cache
  users --alias <name> --account-id <id>  Create a custom alias
  users --list            Show all cached user aliases
  users --clear           Clear the user cache
  link <ISSUE_KEY> --relates <TARGET>   Create a 'Relates' link
  link <ISSUE_KEY> --blocks <TARGET>    Create a 'Blocks' link
  link <ISSUE_KEY> --type <NAME> --to <TARGET>  Create a custom link type
  weblink <ISSUE_KEY> --url <URL> [--title '...']  Attach an external URL to an issue
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
USER_CACHE_PATH = Path.home() / ".sherpa" / "jira_users.json"

SUBTASK_TYPES = {
    "dev": {"summary": "Dev", "issuetype": "Internal Sub-task"},
    "validate": {"summary": "Validate", "issuetype": "Test"},
    "bug": {"summary": "Bug", "issuetype": "Bug subtask"},
}


def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _load_secret(key: str) -> str:
    value = _load_vault().get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _load_default(key: str) -> str | None:
    return _load_vault().get(key)


def _client() -> httpx.Client:
    base_url = _load_secret("JIRA_URL").rstrip("/")
    username = _load_secret("JIRA_USERNAME")
    token = _load_secret("JIRA_API_TOKEN")
    return httpx.Client(
        base_url=base_url,
        auth=(username, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )


def _resolve_project(args_project: str | None) -> str:
    if args_project:
        return args_project
    default = _load_default("JIRA_DEFAULT_PROJECT")
    if default:
        return default
    print("No --project specified and JIRA_DEFAULT_PROJECT not set in vault", file=sys.stderr)
    sys.exit(1)


def _load_user_cache() -> dict:
    return json.loads(USER_CACHE_PATH.read_text()) if USER_CACHE_PATH.exists() else {}


def _save_user_cache(cache: dict) -> None:
    USER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _cache_user(cache: dict, query: str, user: dict) -> None:
    """Cache a user entry keyed by the original query."""
    entry = {
        "accountId": user["accountId"],
        "displayName": user.get("displayName", ""),
        "email": user.get("emailAddress", ""),
    }
    cache[query.lower()] = entry
    _save_user_cache(cache)


def _resolve_account_id(client: httpx.Client, query: str) -> str:
    """Resolve a name, email, or partial match to a Jira accountId.

    Checks the local alias cache first, then falls back to the Jira API.
    Single API matches are auto-cached; multiple matches cause an error
    listing the options so the caller can refine.
    """
    # Cache check
    cache = _load_user_cache()
    cached = cache.get(query.lower())
    if cached:
        return cached["accountId"]

    # API search
    resp = client.get("/rest/api/3/user/search", params={"query": query})
    if resp.status_code != 200:
        print(f"Failed to search users: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(2)
    users = resp.json()

    if not users:
        print(f"No users found for '{query}'", file=sys.stderr)
        sys.exit(2)

    if len(users) == 1:
        _cache_user(cache, query, users[0])
        return users[0]["accountId"]

    # Multiple matches — list them and exit
    print(f"Multiple users match '{query}'. Please refine:", file=sys.stderr)
    for u in users:
        name = u.get("displayName", "?")
        email = u.get("emailAddress", "")
        print(f"  - {name}  {email}", file=sys.stderr)
    sys.exit(2)


def _find_subtask(client: httpx.Client, parent_key: str, type_key: str) -> dict | None:
    """Find a subtask under parent matching the summary prefix for the given type."""
    cfg = SUBTASK_TYPES[type_key]
    prefix = cfg["summary"]
    resp = client.post("/rest/api/3/search/jql", json={
        "jql": f"parent = {parent_key}",
        "maxResults": 50,
        "fields": ["summary", "status", "issuetype"],
    })
    if resp.status_code != 200:
        print(f"Failed to search subtasks: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(2)
    for issue in resp.json().get("issues", []):
        if issue["fields"]["summary"].startswith(prefix):
            return issue
    return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_epic(args: argparse.Namespace) -> None:
    with _client() as client:
        resp = client.put(
            f"/rest/api/3/issue/{args.issue_key}",
            json={"fields": {"customfield_10014": args.epic}},
        )
        if resp.status_code not in (200, 204):
            print(f"Failed to set epic: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": args.issue_key, "epic": args.epic}))


def cmd_subtask(args: argparse.Namespace) -> None:
    cfg = SUBTASK_TYPES[args.type]
    summary = args.summary or cfg["summary"]
    project = _resolve_project(args.project)

    with _client() as client:
        fields: dict = {
            "project": {"key": project},
            "parent": {"key": args.parent_key},
            "summary": summary,
            "issuetype": {"name": cfg["issuetype"]},
        }

        assignee_email = args.assignee
        if not assignee_email and args.type == "dev":
            assignee_email = _load_default("JIRA_DEFAULT_ASSIGNEE")
        if assignee_email:
            fields["assignee"] = {"id": _resolve_account_id(client, assignee_email)}

        resp = client.post("/rest/api/3/issue", json={"fields": fields})
        if resp.status_code not in (200, 201):
            print(f"Failed to create subtask: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        issue = resp.json()
    print(json.dumps({"key": issue["key"], "parent": args.parent_key, "type": args.type, "summary": summary}))


def cmd_complete_subtask(args: argparse.Namespace) -> None:
    with _client() as client:
        subtask = _find_subtask(client, args.parent_key, args.type)
        if not subtask:
            print(f"No '{args.type}' subtask found under {args.parent_key}", file=sys.stderr)
            sys.exit(2)

        key = subtask["key"]

        # Get available transitions
        resp = client.get(f"/rest/api/3/issue/{key}/transitions")
        if resp.status_code != 200:
            print(f"Failed to get transitions: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        transitions = resp.json().get("transitions", [])
        done = next((t for t in transitions if t["name"].lower() == "done"), None)
        if not done:
            available = [t["name"] for t in transitions]
            print(f"No 'Done' transition available for {key}. Options: {available}", file=sys.stderr)
            sys.exit(2)

        resp = client.post(
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": done["id"]}},
        )
        if resp.status_code not in (200, 204):
            print(f"Failed to transition: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": key, "parent": args.parent_key, "status": "Done"}))


def cmd_users(args: argparse.Namespace) -> None:
    if args.clear:
        if USER_CACHE_PATH.exists():
            USER_CACHE_PATH.unlink()
        print("User cache cleared.")
        return

    if args.list:
        cache = _load_user_cache()
        if not cache:
            print("No cached users.")
            return
        for alias, info in cache.items():
            print(f"  {alias} → {info['displayName']}  {info.get('email', '')}")
        return

    # Explicit alias: cache a known accountId under a short name
    if args.alias:
        if not args.account_id:
            print("--alias requires --account-id", file=sys.stderr)
            sys.exit(1)
        cache = _load_user_cache()
        cache[args.alias.lower()] = {
            "accountId": args.account_id,
            "displayName": args.alias,
            "email": "",
        }
        _save_user_cache(cache)
        print(f"Cached alias: {args.alias} → {args.account_id}")
        return

    if not args.query:
        print("Provide a search query, --list, or --clear", file=sys.stderr)
        sys.exit(1)

    with _client() as client:
        resp = client.get("/rest/api/3/user/search", params={"query": args.query})
        if resp.status_code != 200:
            print(f"Failed to search users: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        users = resp.json()

    if not users:
        print(f"No users found for '{args.query}'")
        return

    cache = _load_user_cache()
    results = []
    for idx, u in enumerate(users, 1):
        results.append({
            "index": idx,
            "displayName": u.get("displayName", ""),
            "email": u.get("emailAddress", ""),
            "accountId": u["accountId"],
        })

    # --select N: pick from search results and cache
    if args.select is not None:
        if args.select < 1 or args.select > len(users):
            print(f"--select must be between 1 and {len(users)}", file=sys.stderr)
            sys.exit(1)
        picked = users[args.select - 1]
        _cache_user(cache, args.query, picked)
        print(f"Cached: {args.query} → {picked.get('displayName', '')}", file=sys.stderr)
        print(json.dumps(results[args.select - 1], indent=2))
        return

    if len(users) == 1:
        _cache_user(cache, args.query, users[0])
        print(f"Cached: {args.query} → {users[0].get('displayName', '')}", file=sys.stderr)

    print(json.dumps(results, indent=2))


def cmd_assign_subtask(args: argparse.Namespace) -> None:
    with _client() as client:
        subtask = _find_subtask(client, args.parent_key, args.type)
        if not subtask:
            print(f"No '{args.type}' subtask found under {args.parent_key}", file=sys.stderr)
            sys.exit(2)

        key = subtask["key"]
        account_id = _resolve_account_id(client, args.assignee)

        resp = client.put(
            f"/rest/api/3/issue/{key}",
            json={"fields": {"assignee": {"id": account_id}}},
        )
        if resp.status_code not in (200, 204):
            print(f"Failed to assign: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": key, "parent": args.parent_key, "assignee": args.assignee}))


def cmd_link(args: argparse.Namespace) -> None:
    if args.relates:
        link_type, target = "Relates", args.relates
    elif args.blocks:
        link_type, target = "Blocks", args.blocks
    elif args.link_type and args.to:
        link_type, target = args.link_type, args.to
    else:
        print("Provide --relates, --blocks, or --type with --to", file=sys.stderr)
        sys.exit(1)

    with _client() as client:
        resp = client.post("/rest/api/3/issueLink", json={
            "type": {"name": link_type},
            "inwardIssue": {"key": target},
            "outwardIssue": {"key": args.issue_key},
        })
        if resp.status_code not in (200, 201):
            print(f"Failed to link: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"source": args.issue_key, "target": target, "type": link_type}))


def cmd_weblink(args: argparse.Namespace) -> None:
    title = args.title or args.url
    with _client() as client:
        resp = client.post(
            f"/rest/api/3/issue/{args.issue_key}/remotelink",
            json={"object": {"url": args.url, "title": title}},
        )
        if resp.status_code not in (200, 201):
            print(f"Failed to add web link: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": args.issue_key, "url": args.url, "title": title}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Advanced Jira operations: epic assignment, subtask management, workflow automation.",
        epilog="Defaults loaded from vault: JIRA_DEFAULT_PROJECT, JIRA_DEFAULT_ASSIGNEE",
    )
    sub = parser.add_subparsers(dest="command")

    # epic
    p = sub.add_parser("epic", help="Set an issue's epic")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--epic", required=True, help="Epic key (e.g. KB-100)")

    # subtask
    p = sub.add_parser("subtask", help="Create a typed subtask under a parent issue")
    p.add_argument("parent_key", help="Parent issue key (e.g. KB-123)")
    p.add_argument("--type", required=True, choices=["dev", "validate", "bug"], help="Subtask type")
    p.add_argument("--assignee", default=None, help="Assignee email (dev type auto-assigns from vault)")
    p.add_argument("--summary", default=None, help="Custom summary (default: type name)")
    p.add_argument("--project", default=None, help="Project key (default: vault JIRA_DEFAULT_PROJECT)")

    # complete-subtask
    p = sub.add_parser("complete-subtask", help="Transition a subtask to Done by type")
    p.add_argument("parent_key", help="Parent issue key (e.g. KB-123)")
    p.add_argument("--type", required=True, choices=["dev", "validate"], help="Subtask type to complete")

    # assign-subtask
    p = sub.add_parser("assign-subtask", help="Reassign a subtask by type")
    p.add_argument("parent_key", help="Parent issue key (e.g. KB-123)")
    p.add_argument("--type", required=True, choices=["dev", "validate"], help="Subtask type to reassign")
    p.add_argument("--assignee", required=True, help="New assignee (name, email, or alias)")

    # users
    p = sub.add_parser("users", help="Search Jira users and manage the alias cache")
    p.add_argument("query", nargs="?", default=None, help="Name or email to search")
    p.add_argument("--list", action="store_true", help="Show all cached user aliases")
    p.add_argument("--clear", action="store_true", help="Clear the user cache")
    p.add_argument("--select", type=int, default=None, help="Pick result N from a search to cache (1-based)")
    p.add_argument("--alias", default=None, help="Create a custom alias name")
    p.add_argument("--account-id", default=None, help="Account ID to pair with --alias")

    # link
    p = sub.add_parser("link", help="Link two issues")
    p.add_argument("issue_key", help="Source issue key")
    p.add_argument("--relates", default=None, help="Target for 'Relates' link")
    p.add_argument("--blocks", default=None, help="Target for 'Blocks' link")
    p.add_argument("--type", default=None, dest="link_type", help="Custom link type name")
    p.add_argument("--to", default=None, help="Target for custom --type link")

    # weblink
    p = sub.add_parser("weblink", help="Attach an external URL to an issue")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--url", required=True, help="URL to attach")
    p.add_argument("--title", default=None, help="Link title (defaults to the URL)")

    args = parser.parse_args()

    match args.command:
        case "epic":
            cmd_epic(args)
        case "subtask":
            cmd_subtask(args)
        case "complete-subtask":
            cmd_complete_subtask(args)
        case "assign-subtask":
            cmd_assign_subtask(args)
        case "users":
            cmd_users(args)
        case "link":
            cmd_link(args)
        case "weblink":
            cmd_weblink(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
