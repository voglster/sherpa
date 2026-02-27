#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: jira_admin
description: Advanced Jira operations - epic assignment, subtask management, and workflow automation
categories: [jira, admin, project-management, workflow, subtasks, epic]
secrets:
  - JIRA_URL
  - JIRA_USERNAME
  - JIRA_API_TOKEN
usage: |
  epic <ISSUE_KEY> --epic <EPIC_KEY>
  subtask <PARENT_KEY> --type dev|validate|bug [--assignee email] [--summary '...']
  complete-subtask <PARENT_KEY> --type dev|validate
  assign-subtask <PARENT_KEY> --type dev|validate --assignee <email>
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"

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


def _resolve_account_id(client: httpx.Client, email: str) -> str:
    resp = client.get("/rest/api/3/user/search", params={"query": email})
    if resp.status_code != 200:
        print(f"Failed to search users: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(2)
    users = resp.json()
    if not users:
        print(f"No user found for: {email}", file=sys.stderr)
        sys.exit(2)
    return users[0]["accountId"]


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
            "customfield_10118": [{"value": "GRAVITATE"}],
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
    p.add_argument("--assignee", required=True, help="New assignee email")

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
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
