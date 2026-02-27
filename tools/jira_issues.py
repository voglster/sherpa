#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: jira_issues
description: Create, update, search, and transition Jira issues with sprint support
categories: [jira, tickets, project-management, sprint]
secrets:
  - JIRA_URL
  - JIRA_USERNAME
  - JIRA_API_TOKEN
usage: |
  get <ISSUE_KEY>
  create --summary '<title>' [--project KEY] [--type Bug] [--parent KB-123] [--sprint]
  update <ISSUE_KEY> [--summary '...'] [--description '...'] [--assignee email] [--labels L1 L2]
  transition <ISSUE_KEY> --status '<status>'
  comment <ISSUE_KEY> --body '<markdown text>'
  search [--jql '<JQL>'] [--project KEY] [--status '<status>'] [--mine] [--current-sprint]
  sprints [--board ID]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _load_secret(key: str) -> str:
    value = _load_vault().get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _load_default(key: str) -> str | None:
    """Load an optional vault key, returning None if missing."""
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


def _resolve_board(args_board: int | None) -> int:
    if args_board:
        return args_board
    default = _load_default("JIRA_DEFAULT_BOARD")
    if default:
        return int(default)
    print("No --board specified and JIRA_DEFAULT_BOARD not set in vault", file=sys.stderr)
    sys.exit(1)


def _resolve_assignee(args_assignee: str | None) -> str | None:
    """Return explicit arg, or vault default, or None."""
    if args_assignee:
        return args_assignee
    return _load_default("JIRA_DEFAULT_ASSIGNEE")


# ---------------------------------------------------------------------------
# Markdown -> Atlassian Document Format (ADF) converter (minimal)
# ---------------------------------------------------------------------------

def _md_to_adf(text: str) -> dict:
    """Convert simple markdown to ADF. Supports headings, paragraphs, code blocks, and bullet lists."""
    content = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            lang = line[3:].strip() or None
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            node = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            if lang:
                node["attrs"] = {"language": lang}
            content.append(node)
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": [{"type": "text", "text": m.group(2)}],
            })
            i += 1
            continue

        # Bullet list item (collect consecutive)
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                item_text = re.sub(r"^\s*[-*]\s+", "", lines[i])
                items.append({
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": item_text}],
                    }],
                })
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue

        # Blank line — skip
        if not line.strip():
            i += 1
            continue

        # Plain paragraph
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": line}],
        })
        i += 1

    return {"version": 1, "type": "doc", "content": content} if content else {
        "version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}],
    }


def _adf_to_text(adf: dict | None) -> str:
    """Flatten ADF to plain text for display."""
    if not adf:
        return ""
    parts = []
    for node in adf.get("content", []):
        ntype = node.get("type")
        if ntype in ("paragraph", "heading"):
            text = "".join(c.get("text", "") for c in node.get("content", []))
            if ntype == "heading":
                text = "#" * node.get("attrs", {}).get("level", 1) + " " + text
            parts.append(text)
        elif ntype == "codeBlock":
            code = "".join(c.get("text", "") for c in node.get("content", []))
            parts.append(f"```\n{code}\n```")
        elif ntype == "bulletList":
            for item in node.get("content", []):
                for p in item.get("content", []):
                    text = "".join(c.get("text", "") for c in p.get("content", []))
                    parts.append(f"- {text}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sprint helpers
# ---------------------------------------------------------------------------

def _find_my_sprint(client: httpx.Client, board_id: int) -> dict | None:
    """Find the active sprint matching JIRA_DEFAULT_SPRINT_PREFIX on the given board."""
    prefix = _load_default("JIRA_DEFAULT_SPRINT_PREFIX")
    resp = client.get(f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active"})
    if resp.status_code != 200:
        print(f"Failed to list sprints: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(2)
    sprints = resp.json().get("values", [])
    if prefix:
        match = next((s for s in sprints if s["name"].startswith(prefix)), None)
        if match:
            return match
    # Fall back to first active sprint if no prefix configured or no match
    return sprints[0] if sprints else None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_create(args: argparse.Namespace) -> None:
    project = _resolve_project(args.project)

    # When creating under a parent, default to "Bug subtask" unless explicitly overridden
    issue_type = args.type
    if args.parent and issue_type == "Bug":
        issue_type = "Bug subtask"

    with _client() as client:
        fields: dict = {
            "project": {"key": project},
            "summary": args.summary,
            "issuetype": {"name": issue_type},
        }
        if args.parent:
            fields["parent"] = {"key": args.parent}
        if args.description:
            fields["description"] = _md_to_adf(args.description)

        # Required custom field: Customer (multiselect, defaults to GRAVITATE)
        fields["customfield_10118"] = [{"value": "GRAVITATE"}]

        # Auto-assign if default is set and no explicit opt-out
        assignee_email = _resolve_assignee(None)
        if assignee_email:
            resp = client.get("/rest/api/3/user/search", params={"query": assignee_email})
            if resp.status_code == 200 and resp.json():
                fields["assignee"] = {"id": resp.json()[0]["accountId"]}

        resp = client.post("/rest/api/3/issue", json={"fields": fields})
        if resp.status_code not in (200, 201):
            print(f"Failed to create issue: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        issue = resp.json()
        issue_key = issue["key"]
        print(f"Created {issue_key}", file=sys.stderr)

        # Add to sprint if requested
        if args.sprint:
            board_id = _resolve_board(None)
            sprint = _find_my_sprint(client, board_id)
            if not sprint:
                print("No matching active sprint found", file=sys.stderr)
            else:
                move_resp = client.post(
                    f"/rest/agile/1.0/sprint/{sprint['id']}/issue",
                    json={"issues": [issue_key]},
                )
                if move_resp.status_code not in (200, 204):
                    print(f"Failed to add to sprint: {move_resp.status_code} {move_resp.text}", file=sys.stderr)
                else:
                    print(f"Added to sprint: {sprint['name']}", file=sys.stderr)

        print(json.dumps({"key": issue_key, "id": issue["id"], "self": issue["self"]}))


def cmd_get(args: argparse.Namespace) -> None:
    with _client() as client:
        resp = client.get(f"/rest/api/3/issue/{args.issue_key}")
        if resp.status_code != 200:
            print(f"Failed to fetch issue: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        data = resp.json()
        fields = data["fields"]
        parent = fields.get("parent")
        output = {
            "key": data["key"],
            "summary": fields.get("summary"),
            "status": fields.get("status", {}).get("name"),
            "type": fields.get("issuetype", {}).get("name"),
            "parent": {"key": parent["key"], "summary": parent["fields"]["summary"]} if parent else None,
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "priority": (fields.get("priority") or {}).get("name"),
            "labels": fields.get("labels", []),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "description_text": _adf_to_text(fields.get("description")),
        }
        print(json.dumps(output, indent=2))


def cmd_update(args: argparse.Namespace) -> None:
    fields: dict = {}
    if args.summary:
        fields["summary"] = args.summary
    if args.description:
        fields["description"] = _md_to_adf(args.description)
    if args.labels:
        fields["labels"] = args.labels
    if args.assignee:
        fields["assignee"] = {"id": _resolve_account_id(args.assignee)}

    if not fields:
        print("No fields to update", file=sys.stderr)
        sys.exit(1)

    with _client() as client:
        resp = client.put(f"/rest/api/3/issue/{args.issue_key}", json={"fields": fields})
        if resp.status_code not in (200, 204):
            print(f"Failed to update issue: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": args.issue_key, "status": "updated"}))


def _resolve_account_id(email: str) -> str:
    """Look up a Jira account ID by email address."""
    with _client() as client:
        resp = client.get("/rest/api/3/user/search", params={"query": email})
        if resp.status_code != 200:
            print(f"Failed to search users: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        users = resp.json()
        if not users:
            print(f"No user found for: {email}", file=sys.stderr)
            sys.exit(2)
        return users[0]["accountId"]


def cmd_transition(args: argparse.Namespace) -> None:
    with _client() as client:
        resp = client.get(f"/rest/api/3/issue/{args.issue_key}/transitions")
        if resp.status_code != 200:
            print(f"Failed to get transitions: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        transitions = resp.json().get("transitions", [])
        target = args.status.lower()
        match = next((t for t in transitions if t["name"].lower() == target), None)
        if not match:
            available = [t["name"] for t in transitions]
            print(f"Transition '{args.status}' not available. Options: {available}", file=sys.stderr)
            sys.exit(2)

        resp = client.post(
            f"/rest/api/3/issue/{args.issue_key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        if resp.status_code not in (200, 204):
            print(f"Failed to transition: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
    print(json.dumps({"key": args.issue_key, "status": match["name"]}))


def cmd_search(args: argparse.Namespace) -> None:
    if args.jql:
        jql = args.jql
    else:
        parts = []
        project = args.project or _load_default("JIRA_DEFAULT_PROJECT")
        if project:
            parts.append(f"project = {project}")
        if args.mine:
            parts.append("assignee = currentUser()")
        if args.current_sprint:
            prefix = _load_default("JIRA_DEFAULT_SPRINT_PREFIX")
            if prefix:
                parts.append(f'sprint = "{prefix}" AND sprint in openSprints()')
            else:
                parts.append("sprint in openSprints()")
        if args.status:
            parts.append(f'status = "{args.status}"')
        if not parts:
            print("Provide --jql or at least --project (or set JIRA_DEFAULT_PROJECT)", file=sys.stderr)
            sys.exit(1)
        jql = " AND ".join(parts) + " ORDER BY updated DESC"

    print(f"JQL: {jql}", file=sys.stderr)
    with _client() as client:
        resp = client.post("/rest/api/3/search/jql", json={
            "jql": jql,
            "maxResults": args.max_results,
            "fields": ["summary", "status", "assignee", "issuetype", "priority", "updated"],
        })
        if resp.status_code != 200:
            print(f"Search failed: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        data = resp.json()

    issues = []
    for item in data.get("issues", []):
        f = item["fields"]
        issues.append({
            "key": item["key"],
            "summary": f.get("summary"),
            "status": f.get("status", {}).get("name"),
            "type": f.get("issuetype", {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "priority": (f.get("priority") or {}).get("name"),
            "updated": f.get("updated"),
        })
    print(json.dumps({"total": data.get("total", 0), "issues": issues}, indent=2))


def cmd_sprints(args: argparse.Namespace) -> None:
    board_id = _resolve_board(args.board)

    with _client() as client:
        resp = client.get(
            f"/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active,future"},
        )
        if resp.status_code != 200:
            print(f"Failed to list sprints: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        sprints = resp.json().get("values", [])

    output = []
    for s in sprints:
        output.append({
            "id": s["id"],
            "name": s["name"],
            "state": s["state"],
            "startDate": s.get("startDate"),
            "endDate": s.get("endDate"),
        })
    print(json.dumps({"board_id": board_id, "sprints": output}, indent=2))


def cmd_comment(args: argparse.Namespace) -> None:
    with _client() as client:
        resp = client.post(
            f"/rest/api/3/issue/{args.issue_key}/comment",
            json={"body": _md_to_adf(args.body)},
        )
        if resp.status_code not in (200, 201):
            print(f"Failed to add comment: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        comment = resp.json()
    print(json.dumps({"key": args.issue_key, "comment_id": comment["id"]}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create, update, search, and transition Jira issues.",
        epilog="Defaults loaded from vault: JIRA_DEFAULT_PROJECT, JIRA_DEFAULT_BOARD, "
               "JIRA_DEFAULT_SPRINT_PREFIX, JIRA_DEFAULT_ASSIGNEE",
    )
    sub = parser.add_subparsers(dest="command")

    # create
    p = sub.add_parser("create", help="Create an issue (optionally in your active sprint)")
    p.add_argument("--project", default=None, help="Project key (default: vault JIRA_DEFAULT_PROJECT)")
    p.add_argument("--summary", required=True, help="Issue summary/title")
    p.add_argument("--description", default=None, help="Markdown description")
    p.add_argument("--type", default="Bug", help="Issue type (default: Bug, or Bug subtask when --parent is set)")
    p.add_argument("--parent", default=None, help="Parent issue key for subtasks (e.g. KB-41269)")
    p.add_argument("--sprint", action="store_true", help="Add to your active sprint")

    # get
    p = sub.add_parser("get", help="Fetch issue details by key")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")

    # update
    p = sub.add_parser("update", help="Update fields on an existing issue")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--summary", default=None, help="New summary")
    p.add_argument("--description", default=None, help="New markdown description")
    p.add_argument("--assignee", default=None, help="Assignee email")
    p.add_argument("--labels", nargs="+", default=None, help="Labels to set")

    # transition
    p = sub.add_parser("transition", help="Move issue to a new status")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--status", required=True, help="Target status name (e.g. Done)")

    # search
    p = sub.add_parser("search", help="Search issues with JQL or convenience filters")
    p.add_argument("--jql", default=None, help="Raw JQL query (overrides other filters)")
    p.add_argument("--project", default=None, help="Filter by project (default: vault)")
    p.add_argument("--status", default=None, help="Filter by status")
    p.add_argument("--mine", action="store_true", help="Only issues assigned to me")
    p.add_argument("--current-sprint", action="store_true", help="Only issues in my active sprint")
    p.add_argument("--max-results", type=int, default=20, help="Max results (default: 20)")

    # comment
    p = sub.add_parser("comment", help="Add a comment to an issue")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--body", required=True, help="Comment body (markdown)")

    # sprints
    p = sub.add_parser("sprints", help="List active/future sprints for your board")
    p.add_argument("--board", type=int, default=None, help="Board ID (default: vault JIRA_DEFAULT_BOARD)")

    # Check for admin commands before argparse rejects them
    admin_commands = {"epic", "subtask", "complete-subtask", "assign-subtask"}
    if len(sys.argv) > 1 and sys.argv[1] in admin_commands:
        cmd = sys.argv[1]
        print(
            f"Hint: '{cmd}' is available in jira_admin. "
            f"Run: uv run tools/jira_admin.py {cmd} --help",
            file=sys.stderr,
        )
        sys.exit(1)

    args = parser.parse_args()

    match args.command:
        case "create":
            cmd_create(args)
        case "get":
            cmd_get(args)
        case "update":
            cmd_update(args)
        case "transition":
            cmd_transition(args)
        case "search":
            cmd_search(args)
        case "sprints":
            cmd_sprints(args)
        case "comment":
            cmd_comment(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
