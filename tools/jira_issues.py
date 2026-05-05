#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "markdown-it-py"]
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
  create --summary '<title>' [--description '...'|--description-file PATH|--description-stdin]
         [--project KEY] [--type Bug] [--parent KB-123] [--sprint] [--dry-run]
  update <ISSUE_KEY> [--summary '...'] [--description '...'|--description-file PATH|--description-stdin]
         [--assignee name] [--labels L1 L2] [--sprint] [--dry-run]
  transition <ISSUE_KEY> --status '<status>'
  comment <ISSUE_KEY> [--body '...'|--body-file PATH|--body-stdin] [--dry-run]
  search [--jql '<JQL>'] [--project KEY] [--status '<status>'] [--mine] [--current-sprint]
  sprints [--board ID]
  Body input: --description / --body accept '@PATH' shorthand to read from a file.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import httpx
from markdown_it import MarkdownIt

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
USER_CACHE_PATH = Path.home() / ".sherpa" / "jira_users.json"


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
# Body input resolution (inline / file / stdin / @PATH)
# ---------------------------------------------------------------------------

_SHELL_SUBST_RE = re.compile(r"^\s*\$\([^)]*\)\s*$|^\s*\$\(cat\b")


def _resolve_body(
    *,
    inline: str | None,
    file_path: str | None,
    use_stdin: bool,
    label: str,
) -> str | None:
    """Resolve a free-text body from one of: inline (--description / --body),
    a file (--description-file / --body-file or `@PATH` shorthand on inline),
    or stdin (--description-stdin / --body-stdin).

    Returns None if no source provided. Errors if more than one is provided
    or if the resolved body looks like an unexpanded shell substitution.
    """
    sources_used = sum(x is not None and x is not False for x in (inline, file_path, use_stdin or None))
    # @PATH shorthand on the inline arg redirects to file
    if inline and inline.startswith("@") and not file_path and not use_stdin:
        file_path = inline[1:]
        inline = None
        sources_used = 1

    if sources_used == 0:
        return None
    if sources_used > 1:
        print(
            f"Provide only one of --{label}, --{label}-file, --{label}-stdin",
            file=sys.stderr,
        )
        sys.exit(1)

    if file_path:
        try:
            body = Path(file_path).read_text()
        except OSError as e:
            print(f"Failed to read {file_path}: {e}", file=sys.stderr)
            sys.exit(1)
    elif use_stdin:
        body = sys.stdin.read()
    else:
        body = inline or ""

    if not body.strip():
        print(f"--{label} body is empty or whitespace", file=sys.stderr)
        sys.exit(1)
    if _SHELL_SUBST_RE.search(body.strip().splitlines()[0] if body.strip() else ""):
        print(
            f"--{label} body looks like an unexpanded shell substitution "
            f"(starts with $(...)). Did you mean --{label}-file PATH?",
            file=sys.stderr,
        )
        sys.exit(1)
    return body


# ---------------------------------------------------------------------------
# Markdown -> Atlassian Document Format (ADF) converter (minimal)
# ---------------------------------------------------------------------------

_MD_PARSER = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable("strikethrough")


def _md_to_adf(text: str) -> dict:
    """Convert CommonMark markdown to ADF using markdown-it-py.

    Supports: headings, paragraphs, hard/soft line breaks, bold/italic/strike,
    inline code, links, bullet + ordered + nested lists, fenced + indented code
    blocks, blockquotes, and thematic breaks. Unknown constructs (tables,
    images) fall back to a best-effort textual representation.
    """
    tokens = _MD_PARSER.parse(text or "")
    content = _walk_blocks(tokens, 0, len(tokens))
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"version": 1, "type": "doc", "content": content}


def _find_close(tokens, start: int, open_type: str, close_type: str) -> int:
    depth = 0
    for k in range(start, len(tokens)):
        if tokens[k].type == open_type:
            depth += 1
        elif tokens[k].type == close_type:
            depth -= 1
            if depth == 0:
                return k
    return len(tokens) - 1


def _walk_blocks(tokens, start: int, end: int) -> list:
    nodes: list = []
    i = start
    while i < end:
        t = tokens[i]
        if t.type == "heading_open":
            level = int(t.tag[1])
            inline = tokens[i + 1].children or []
            nodes.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _walk_inline(inline),
            })
            i += 3
        elif t.type == "paragraph_open":
            inline = tokens[i + 1].children or []
            content = _walk_inline(inline) or [{"type": "text", "text": ""}]
            nodes.append({"type": "paragraph", "content": content})
            i += 3
        elif t.type in ("fence", "code_block"):
            lang = (t.info or "").strip()
            code = (t.content or "").rstrip("\n")
            node: dict = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            if code:
                node["content"] = [{"type": "text", "text": code}]
            nodes.append(node)
            i += 1
        elif t.type == "bullet_list_open":
            j = _find_close(tokens, i, "bullet_list_open", "bullet_list_close")
            nodes.append({"type": "bulletList", "content": _walk_list_items(tokens, i + 1, j)})
            i = j + 1
        elif t.type == "ordered_list_open":
            j = _find_close(tokens, i, "ordered_list_open", "ordered_list_close")
            node = {"type": "orderedList", "content": _walk_list_items(tokens, i + 1, j)}
            start_attr = (dict(t.attrs).get("start") if t.attrs else None)
            if start_attr and str(start_attr) != "1":
                node["attrs"] = {"order": int(start_attr)}
            nodes.append(node)
            i = j + 1
        elif t.type == "blockquote_open":
            j = _find_close(tokens, i, "blockquote_open", "blockquote_close")
            inner = _walk_blocks(tokens, i + 1, j)
            nodes.append({"type": "blockquote", "content": inner or [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]})
            i = j + 1
        elif t.type == "hr":
            nodes.append({"type": "rule"})
            i += 1
        else:
            i += 1
    return nodes


def _walk_list_items(tokens, start: int, end: int) -> list:
    items = []
    i = start
    while i < end:
        if tokens[i].type == "list_item_open":
            j = _find_close(tokens, i, "list_item_open", "list_item_close")
            inner = _walk_blocks(tokens, i + 1, j)
            items.append({"type": "listItem", "content": inner or [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]})
            i = j + 1
        else:
            i += 1
    return items


def _walk_inline(tokens) -> list:
    nodes: list = []
    mark_stack: list = []

    def text_node(s: str) -> dict:
        node = {"type": "text", "text": s}
        if mark_stack:
            node["marks"] = [dict(m) for m in mark_stack]
        return node

    for t in tokens:
        ttype = t.type
        if ttype == "text":
            if t.content:
                nodes.append(text_node(t.content))
        elif ttype in ("softbreak", "hardbreak"):
            nodes.append({"type": "hardBreak"})
        elif ttype == "code_inline":
            mark_stack.append({"type": "code"})
            nodes.append(text_node(t.content))
            mark_stack.pop()
        elif ttype == "em_open":
            mark_stack.append({"type": "em"})
        elif ttype == "em_close":
            mark_stack.pop()
        elif ttype == "strong_open":
            mark_stack.append({"type": "strong"})
        elif ttype == "strong_close":
            mark_stack.pop()
        elif ttype == "s_open":
            mark_stack.append({"type": "strike"})
        elif ttype == "s_close":
            mark_stack.pop()
        elif ttype == "link_open":
            href = (dict(t.attrs).get("href") if t.attrs else "") or ""
            mark_stack.append({"type": "link", "attrs": {"href": href}})
        elif ttype == "link_close":
            mark_stack.pop()
        elif ttype == "image":
            alt = t.content or ""
            if alt:
                nodes.append(text_node(alt))
    return nodes


# ---------------------------------------------------------------------------
# ADF -> markdown (for round-trip preview / cmd_get display)
# ---------------------------------------------------------------------------

def _adf_to_text(adf: dict | None) -> str:
    if not adf:
        return ""
    return "\n\n".join(_render_block(n) for n in adf.get("content", []) if _render_block(n) is not None)


def _render_inline(nodes: list) -> str:
    out = []
    for c in nodes or []:
        ctype = c.get("type")
        if ctype == "hardBreak":
            out.append("\n")
            continue
        if ctype != "text":
            continue
        text = c.get("text", "")
        for mark in c.get("marks", []):
            mtype = mark.get("type")
            if mtype == "code":
                text = f"`{text}`"
            elif mtype == "strong":
                text = f"**{text}**"
            elif mtype == "em":
                text = f"*{text}*"
            elif mtype == "strike":
                text = f"~~{text}~~"
            elif mtype == "link":
                href = mark.get("attrs", {}).get("href", "")
                text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _render_list_item(item: dict, prefix: str) -> str:
    blocks = item.get("content", []) or []
    if not blocks:
        return prefix.rstrip()
    lines: list[str] = []
    indent = " " * len(prefix)
    for idx, b in enumerate(blocks):
        rendered = _render_block(b) or ""
        block_lines = rendered.split("\n")
        if idx == 0:
            lines.append(prefix + block_lines[0])
            for l in block_lines[1:]:
                lines.append(indent + l)
        else:
            # Tight rendering for nested lists; blank separator for paragraphs.
            if b.get("type") not in ("bulletList", "orderedList"):
                lines.append("")
            for l in block_lines:
                lines.append(indent + l if l else "")
    return "\n".join(lines)


def _render_block(node: dict) -> str:
    ntype = node.get("type")
    if ntype == "paragraph":
        return _render_inline(node.get("content", []))
    if ntype == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return f"{'#' * level} {_render_inline(node.get('content', []))}"
    if ntype == "codeBlock":
        code = "".join(c.get("text", "") for c in node.get("content", []))
        lang = node.get("attrs", {}).get("language", "")
        return f"```{lang}\n{code}\n```"
    if ntype == "bulletList":
        return "\n".join(_render_list_item(item, "- ") for item in node.get("content", []))
    if ntype == "orderedList":
        start = node.get("attrs", {}).get("order", 1)
        return "\n".join(
            _render_list_item(item, f"{idx}. ")
            for idx, item in enumerate(node.get("content", []), start=start)
        )
    if ntype == "blockquote":
        inner = "\n\n".join(_render_block(c) for c in node.get("content", []))
        return "\n".join(f"> {ln}" if ln else ">" for ln in inner.split("\n"))
    if ntype == "rule":
        return "---"
    return ""


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


def _echo_roundtrip(client: httpx.Client, issue_key: str) -> None:
    """Fetch the issue and print description_text round-trip preview to stderr."""
    try:
        resp = client.get(f"/rest/api/3/issue/{issue_key}", params={"fields": "description"})
        if resp.status_code != 200:
            return
        adf = resp.json().get("fields", {}).get("description")
        rendered = _adf_to_text(adf)
    except Exception:
        return
    print(f"--- Round-trip preview ({issue_key}) ---", file=sys.stderr)
    print(rendered, file=sys.stderr)
    print("--- end preview ---", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_create(args: argparse.Namespace) -> None:
    project = _resolve_project(args.project)
    description = _resolve_body(
        inline=args.description,
        file_path=args.description_file,
        use_stdin=args.description_stdin,
        label="description",
    )

    # When creating under a parent, default to "Bug subtask" unless explicitly overridden
    issue_type = args.type
    if args.parent and issue_type == "Bug":
        issue_type = "Bug subtask"

    fields: dict = {
        "project": {"key": project},
        "summary": args.summary,
        "issuetype": {"name": issue_type},
    }
    if args.parent:
        fields["parent"] = {"key": args.parent}
    if description is not None:
        fields["description"] = _md_to_adf(description)

    if args.dry_run:
        print(json.dumps({"fields": fields, "sprint": bool(args.sprint)}, indent=2))
        return

    with _client() as client:

        # Required custom field: Customer (multiselect) — top-level issues only
        if not args.parent:
            customer_field = _load_default("JIRA_CUSTOMER_FIELD_ID")
            customer_value = _load_default("JIRA_CUSTOMER_VALUE")
            if customer_field and customer_value:
                fields[customer_field] = [{"value": customer_value}]

        # Auto-assign if default is set and no explicit opt-out
        assignee_email = _resolve_assignee(None)
        if assignee_email:
            fields["assignee"] = {"id": _resolve_account_id(assignee_email)}

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

        if description is not None and not args.no_echo:
            _echo_roundtrip(client, issue_key)

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
        links = []
        for link in fields.get("issuelinks", []):
            link_type = link.get("type", {})
            if "outwardIssue" in link:
                issue = link["outwardIssue"]
                links.append({
                    "direction": link_type.get("outward", ""),
                    "key": issue["key"],
                    "summary": issue["fields"].get("summary", ""),
                    "status": issue["fields"].get("status", {}).get("name", ""),
                })
            elif "inwardIssue" in link:
                issue = link["inwardIssue"]
                links.append({
                    "direction": link_type.get("inward", ""),
                    "key": issue["key"],
                    "summary": issue["fields"].get("summary", ""),
                    "status": issue["fields"].get("status", {}).get("name", ""),
                })
        output["links"] = links

        # Fetch remote (web) links
        rl_resp = client.get(f"/rest/api/3/issue/{args.issue_key}/remotelink")
        web_links = []
        if rl_resp.status_code == 200:
            for rl in rl_resp.json():
                obj = rl.get("object", {})
                web_links.append({"title": obj.get("title", ""), "url": obj.get("url", "")})
        output["web_links"] = web_links

        print(json.dumps(output, indent=2))


def cmd_update(args: argparse.Namespace) -> None:
    description = _resolve_body(
        inline=args.description,
        file_path=args.description_file,
        use_stdin=args.description_stdin,
        label="description",
    )

    fields: dict = {}
    if args.summary:
        fields["summary"] = args.summary
    if description is not None:
        fields["description"] = _md_to_adf(description)
    if args.labels:
        fields["labels"] = args.labels
    if args.assignee:
        fields["assignee"] = {"id": _resolve_account_id(args.assignee)}

    if not fields and not args.sprint:
        print("No fields to update", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(json.dumps({"issue_key": args.issue_key, "fields": fields, "sprint": bool(args.sprint)}, indent=2))
        return

    with _client() as client:
        if fields:
            resp = client.put(f"/rest/api/3/issue/{args.issue_key}", json={"fields": fields})
            if resp.status_code not in (200, 204):
                print(f"Failed to update issue: {resp.status_code} {resp.text}", file=sys.stderr)
                sys.exit(2)

        if args.sprint:
            board_id = _resolve_board(None)
            sprint = _find_my_sprint(client, board_id)
            if not sprint:
                print("No matching active sprint found", file=sys.stderr)
                sys.exit(2)
            move_resp = client.post(
                f"/rest/agile/1.0/sprint/{sprint['id']}/issue",
                json={"issues": [args.issue_key]},
            )
            if move_resp.status_code not in (200, 204):
                print(f"Failed to add to sprint: {move_resp.status_code} {move_resp.text}", file=sys.stderr)
                sys.exit(2)
            print(f"Added to sprint: {sprint['name']}", file=sys.stderr)

        if description is not None and not args.no_echo:
            _echo_roundtrip(client, args.issue_key)

    print(json.dumps({"key": args.issue_key, "status": "updated"}))


def _load_user_cache() -> dict:
    return json.loads(USER_CACHE_PATH.read_text()) if USER_CACHE_PATH.exists() else {}


def _save_user_cache(cache: dict) -> None:
    USER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _resolve_account_id(query: str) -> str:
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
    with _client() as client:
        resp = client.get("/rest/api/3/user/search", params={"query": query})
        if resp.status_code != 200:
            print(f"Failed to search users: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)
        users = resp.json()

    if not users:
        print(f"No users found for '{query}'", file=sys.stderr)
        sys.exit(2)

    if len(users) == 1:
        entry = {
            "accountId": users[0]["accountId"],
            "displayName": users[0].get("displayName", ""),
            "email": users[0].get("emailAddress", ""),
        }
        cache[query.lower()] = entry
        _save_user_cache(cache)
        return users[0]["accountId"]

    # Multiple matches — list them and exit
    print(f"Multiple users match '{query}'. Please refine:", file=sys.stderr)
    for u in users:
        name = u.get("displayName", "?")
        email = u.get("emailAddress", "")
        print(f"  - {name}  {email}", file=sys.stderr)
    sys.exit(2)


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
    body = _resolve_body(
        inline=args.body,
        file_path=args.body_file,
        use_stdin=args.body_stdin,
        label="body",
    )
    if body is None:
        print("Provide --body, --body-file, or --body-stdin", file=sys.stderr)
        sys.exit(1)

    adf = _md_to_adf(body)
    if args.dry_run:
        print(json.dumps({"issue_key": args.issue_key, "body": adf}, indent=2))
        return

    with _client() as client:
        resp = client.post(
            f"/rest/api/3/issue/{args.issue_key}/comment",
            json={"body": adf},
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
    p.add_argument("--description", default=None, help="Markdown description (or '@PATH' to read from file)")
    p.add_argument("--description-file", default=None, help="Read markdown description from file")
    p.add_argument("--description-stdin", action="store_true", help="Read markdown description from stdin")
    p.add_argument("--type", default="Bug", help="Issue type (default: Bug, or Bug subtask when --parent is set)")
    p.add_argument("--parent", default=None, help="Parent issue key for subtasks (e.g. KB-41269)")
    p.add_argument("--sprint", action="store_true", help="Add to your active sprint")
    p.add_argument("--dry-run", action="store_true", help="Print the rendered ADF payload without calling Jira")
    p.add_argument("--no-echo", action="store_true", help="Skip the round-trip description preview after create")

    # get
    p = sub.add_parser("get", help="Fetch issue details by key")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")

    # update
    p = sub.add_parser("update", help="Update fields on an existing issue")
    p.add_argument("issue_key", help="Issue key (e.g. KB-123)")
    p.add_argument("--summary", default=None, help="New summary")
    p.add_argument("--description", default=None, help="New markdown description (or '@PATH' to read from file)")
    p.add_argument("--description-file", default=None, help="Read new markdown description from file")
    p.add_argument("--description-stdin", action="store_true", help="Read new markdown description from stdin")
    p.add_argument("--assignee", default=None, help="Assignee email")
    p.add_argument("--labels", nargs="+", default=None, help="Labels to set")
    p.add_argument("--sprint", action="store_true", help="Move issue to your active sprint")
    p.add_argument("--dry-run", action="store_true", help="Print the rendered payload without calling Jira")
    p.add_argument("--no-echo", action="store_true", help="Skip the round-trip description preview after update")

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
    p.add_argument("--body", default=None, help="Comment body markdown (or '@PATH' to read from file)")
    p.add_argument("--body-file", default=None, help="Read comment body from file")
    p.add_argument("--body-stdin", action="store_true", help="Read comment body from stdin")
    p.add_argument("--dry-run", action="store_true", help="Print the rendered ADF payload without calling Jira")

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
