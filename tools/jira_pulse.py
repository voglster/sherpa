#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: jira_pulse
description: High-level pulse of Jira project activity - who did what, epic progress, status movements over a time window
categories: [jira, project-management, reporting, pulse, activity]
secrets:
  - JIRA_URL
  - JIRA_USERNAME
  - JIRA_API_TOKEN
usage: |
  pulse [--project KEY] [--days 7]
  epic-progress [--project KEY]
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
CONFIG_PATH = Path.home() / ".sherpa" / "jira_pulse_config.json"


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


# ---------------------------------------------------------------------------
# Field discovery
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def _discover_fields(client: httpx.Client) -> dict:
    """Discover custom field IDs for Energy Points and Epic Link.

    Checks vault overrides first, then queries the Jira field API,
    caching results in ~/.sherpa/jira_pulse_config.json.
    """
    config = _load_config()

    # Check vault overrides
    points_field = _load_default("JIRA_POINTS_FIELD_ID")
    epic_field = _load_default("JIRA_EPIC_FIELD_ID")

    if points_field:
        config["points_field"] = points_field
    if epic_field:
        config["epic_field"] = epic_field

    # Return cached if both are present
    if config.get("points_field") and config.get("epic_field"):
        return config

    # Query field metadata
    print("Discovering custom field IDs...", file=sys.stderr)
    resp = client.get("/rest/api/3/field")
    if resp.status_code != 200:
        print(f"Failed to fetch fields: {resp.status_code} {resp.text}", file=sys.stderr)
        # Fall back to defaults
        config.setdefault("points_field", None)
        config.setdefault("epic_field", "customfield_10014")
        _save_config(config)
        return config

    fields = resp.json()
    for f in fields:
        name_lower = f.get("name", "").lower()
        fid = f.get("id", "")
        if not config.get("points_field"):
            if "energy point" in name_lower or name_lower == "story points":
                config["points_field"] = fid
                print(f"  Found points field: {fid} ({f['name']})", file=sys.stderr)
        if not config.get("epic_field"):
            if name_lower == "epic link":
                config["epic_field"] = fid
                print(f"  Found epic field: {fid} ({f['name']})", file=sys.stderr)

    config.setdefault("points_field", None)
    config.setdefault("epic_field", "customfield_10014")
    _save_config(config)

    if not config["points_field"]:
        print("  Warning: Energy Points field not found, points will be 0", file=sys.stderr)

    return config


# ---------------------------------------------------------------------------
# Paginated search
# ---------------------------------------------------------------------------

def _paginated_search(
    client: httpx.Client,
    jql: str,
    fields: list[str],
    expand: str | None = None,
    max_total: int = 500,
) -> list[dict]:
    """Fetch issues via POST /rest/api/3/search/jql with cursor-based pagination."""
    all_issues: list[dict] = []
    next_page_token: str | None = None

    while len(all_issues) < max_total:
        body: dict = {
            "jql": jql,
            "maxResults": min(100, max_total - len(all_issues)),
            "fields": fields,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        if expand:
            body["expand"] = expand

        resp = client.post("/rest/api/3/search/jql", json=body)
        if resp.status_code != 200:
            print(f"Search failed: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(2)

        data = resp.json()
        issues = data.get("issues", [])
        total = data.get("total", 0)
        all_issues.extend(issues)

        print(f"  Fetched {len(all_issues)}/{total} issues...", file=sys.stderr)

        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues:
            break

    if len(all_issues) >= max_total and data.get("total", 0) > max_total:
        print(
            f"  Warning: truncated at {max_total} issues (total: {data['total']})",
            file=sys.stderr,
        )

    return all_issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_points(issue: dict, points_field: str | None) -> float:
    """Extract energy points from an issue, returning 0 if missing."""
    if not points_field:
        return 0
    val = issue.get("fields", {}).get(points_field)
    if val is None:
        return 0
    return float(val)


def _get_epic_key(issue: dict, epic_field: str | None) -> str | None:
    """Get the epic key for an issue, trying custom field then parent."""
    fields = issue.get("fields", {})

    # Try custom epic link field
    if epic_field:
        epic_val = fields.get(epic_field)
        if epic_val:
            return epic_val if isinstance(epic_val, str) else None

    # Fall back to parent if parent is an Epic
    parent = fields.get("parent")
    if parent:
        parent_type = parent.get("fields", {}).get("issuetype", {}).get("name", "")
        if parent_type == "Epic":
            return parent.get("key")

    return None


def _extract_transitions(issue: dict, since: datetime) -> list[dict]:
    """Extract status transitions from changelog that occurred within the time window."""
    transitions = []
    changelog = issue.get("changelog", {})
    for history in changelog.get("histories", []):
        created_str = history.get("created", "")
        if not created_str:
            continue
        # Parse Jira timestamp (e.g. 2026-03-10T14:30:00.000+0000)
        try:
            created = datetime.fromisoformat(created_str.replace("+0000", "+00:00"))
        except ValueError:
            continue
        if created < since:
            continue
        for item in history.get("items", []):
            if item.get("field") == "status":
                transitions.append({
                    "key": issue["key"],
                    "from": item.get("fromString", ""),
                    "to": item.get("toString", ""),
                    "date": created_str,
                    "points": _get_points(issue, issue.get("_points_field")),
                })
    return transitions


# ---------------------------------------------------------------------------
# pulse command
# ---------------------------------------------------------------------------

def cmd_pulse(args: argparse.Namespace) -> None:
    project = _resolve_project(args.project)
    days = args.days
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_str = since.strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    with _client() as client:
        config = _discover_fields(client)
        points_field = config.get("points_field")
        epic_field = config.get("epic_field")

        # Build fields list
        search_fields = [
            "summary", "status", "assignee", "issuetype", "created",
            "updated", "parent",
        ]
        if points_field:
            search_fields.append(points_field)
        if epic_field:
            search_fields.append(epic_field)

        # Fetch updated issues with changelogs
        jql = f"project = {project} AND updated >= '{since_str}' ORDER BY updated DESC"
        print(f"JQL: {jql}", file=sys.stderr)
        issues = _paginated_search(client, jql, search_fields, expand="changelog")

    # Tag issues with points_field for transition extraction
    for issue in issues:
        issue["_points_field"] = points_field

    # Classify issues
    created_issues = []
    completed_issues = []
    all_transitions = []

    for issue in issues:
        fields = issue.get("fields", {})

        # Created in window?
        created_str = fields.get("created", "")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("+0000", "+00:00"))
                if created_dt >= since:
                    created_issues.append(issue)
            except ValueError:
                pass

        # Extract transitions
        transitions = _extract_transitions(issue, since)
        all_transitions.extend(transitions)

        # Completed = has a transition to Done/Closed in this window
        for t in transitions:
            if t["to"].lower() in ("done", "closed"):
                completed_issues.append(issue)
                break

    # Summary stats
    total_points_completed = sum(_get_points(i, points_field) for i in completed_issues)
    summary = {
        "issues_updated": len(issues),
        "issues_created": len(created_issues),
        "issues_completed": len(completed_issues),
        "points_completed": total_points_completed,
    }

    # Assignee breakdown
    assignee_data: dict[str, dict] = defaultdict(lambda: {
        "issues_touched": 0,
        "issues_completed": 0,
        "points_completed": 0,
        "key_issues": [],
    })
    completed_keys = {i["key"] for i in completed_issues}
    for issue in issues:
        assignee = (issue.get("fields", {}).get("assignee") or {}).get("displayName", "(unassigned)")
        data = assignee_data[assignee]
        data["issues_touched"] += 1
        pts = _get_points(issue, points_field)
        if issue["key"] in completed_keys:
            data["issues_completed"] += 1
            data["points_completed"] += pts
        # Track key issues: high-point items plus a few others, capped at 5
        if len(data["key_issues"]) < 5 or pts >= 3:
            if issue["key"] not in data["key_issues"]:
                data["key_issues"].append(issue["key"])
    # Cap key_issues to 5 per assignee
    for data in assignee_data.values():
        data["key_issues"] = data["key_issues"][:5]

    assignees = sorted(
        [
            {"name": name, **vals}
            for name, vals in assignee_data.items()
        ],
        key=lambda x: x["points_completed"],
        reverse=True,
    )

    # Status movements aggregation
    movement_agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "points": 0})
    for t in all_transitions:
        key = (t["from"], t["to"])
        movement_agg[key]["count"] += 1
        movement_agg[key]["points"] += t["points"]

    status_movements = sorted(
        [
            {"from": k[0], "to": k[1], "count": v["count"], "points": v["points"]}
            for k, v in movement_agg.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # Epic progress
    epic_issues: dict[str, list[dict]] = defaultdict(list)
    epic_summaries: dict[str, str] = {}
    for issue in issues:
        ek = _get_epic_key(issue, epic_field)
        if ek:
            epic_issues[ek].append(issue)
            # If the issue itself is the epic, grab its summary
            if issue["key"] == ek:
                epic_summaries[ek] = issue.get("fields", {}).get("summary", "")

    # Fetch summaries for epics not already in results
    missing_epics = [ek for ek in epic_issues if ek not in epic_summaries]
    if missing_epics:
        with _client() as client2:
            epic_jql = f"key IN ({','.join(missing_epics)})"
            print(f"Fetching {len(missing_epics)} epic summaries...", file=sys.stderr)
            epic_data = _paginated_search(client2, epic_jql, ["summary"], max_total=len(missing_epics))
            for ep in epic_data:
                epic_summaries[ep["key"]] = ep.get("fields", {}).get("summary", "")

    epics = []
    for ek, children in epic_issues.items():
        total_count = len(children)
        done_count = sum(
            1 for c in children
            if (c.get("fields", {}).get("status", {}).get("name", "")).lower() in ("done", "closed")
        )
        total_pts = sum(_get_points(c, points_field) for c in children)
        done_pts = sum(
            _get_points(c, points_field)
            for c in children
            if (c.get("fields", {}).get("status", {}).get("name", "")).lower() in ("done", "closed")
        )
        pts_in_period = sum(
            _get_points(c, points_field)
            for c in children
            if c["key"] in completed_keys
        )
        epics.append({
            "key": ek,
            "summary": epic_summaries.get(ek, ""),
            "done": done_count,
            "total": total_count,
            "pct_count": round(done_count / total_count * 100) if total_count else 0,
            "points_done": done_pts,
            "points_total": total_pts,
            "pct_points": round(done_pts / total_pts * 100) if total_pts else 0,
            "points_completed_in_period": pts_in_period,
        })

    epics.sort(key=lambda x: x["points_completed_in_period"], reverse=True)

    # Highlights
    highlights = []
    # Big completions (>= 5 points)
    for issue in completed_issues:
        pts = _get_points(issue, points_field)
        if pts >= 5:
            highlights.append({
                "type": "big_completion",
                "key": issue["key"],
                "summary": issue.get("fields", {}).get("summary", ""),
                "points": pts,
            })
    highlights.sort(key=lambda x: x.get("points", 0), reverse=True)

    # Epic milestones (>= 75% complete)
    for ep in epics:
        if ep["pct_count"] >= 75 and ep["points_completed_in_period"] > 0:
            highlights.append({
                "type": "epic_milestone",
                "key": ep["key"],
                "summary": ep["summary"],
                "pct_count": ep["pct_count"],
                "pct_points": ep["pct_points"],
            })

    output = {
        "project": project,
        "period": {"days": days, "from": since_str, "to": today_str},
        "summary": summary,
        "assignees": assignees,
        "status_movements": status_movements,
        "epics": epics,
        "highlights": highlights,
    }

    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# epic-progress command
# ---------------------------------------------------------------------------

def cmd_epic_progress(args: argparse.Namespace) -> None:
    project = _resolve_project(args.project)

    with _client() as client:
        config = _discover_fields(client)
        points_field = config.get("points_field")

        # Fetch all non-Done epics
        jql = (
            f"project = {project} AND issuetype = Epic "
            f"AND status NOT IN (Done, Closed) ORDER BY rank ASC"
        )
        print(f"Fetching active epics: {jql}", file=sys.stderr)
        epic_issues = _paginated_search(
            client, jql,
            fields=["summary", "status", "assignee"],
        )

        if not epic_issues:
            print(json.dumps({"project": project, "epics": []}))
            return

        # For each epic, fetch children
        epics = []
        for epic in epic_issues:
            epic_key = epic["key"]
            epic_summary = epic.get("fields", {}).get("summary", "")
            epic_status = epic.get("fields", {}).get("status", {}).get("name", "")

            child_fields = ["summary", "status", "assignee"]
            if points_field:
                child_fields.append(points_field)

            children_jql = f"parent = {epic_key} ORDER BY status ASC"
            print(f"  Fetching children of {epic_key}...", file=sys.stderr)
            children = _paginated_search(client, children_jql, child_fields, max_total=200)

            total_count = len(children)
            done_count = sum(
                1 for c in children
                if (c.get("fields", {}).get("status", {}).get("name", "")).lower() in ("done", "closed")
            )
            total_pts = sum(_get_points(c, points_field) for c in children)
            done_pts = sum(
                _get_points(c, points_field)
                for c in children
                if (c.get("fields", {}).get("status", {}).get("name", "")).lower() in ("done", "closed")
            )

            # Contributors
            contributors: dict[str, int] = defaultdict(int)
            for c in children:
                name = (c.get("fields", {}).get("assignee") or {}).get("displayName", "(unassigned)")
                contributors[name] += 1

            epics.append({
                "key": epic_key,
                "summary": epic_summary,
                "status": epic_status,
                "total": total_count,
                "done": done_count,
                "pct_count": round(done_count / total_count * 100) if total_count else 0,
                "points_total": total_pts,
                "points_done": done_pts,
                "pct_points": round(done_pts / total_pts * 100) if total_pts else 0,
                "contributors": dict(contributors),
            })

    print(json.dumps({"project": project, "epics": epics}, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="High-level pulse of Jira project activity over a time window.",
        epilog="Defaults loaded from vault: JIRA_DEFAULT_PROJECT, JIRA_POINTS_FIELD_ID, JIRA_EPIC_FIELD_ID",
    )
    sub = parser.add_subparsers(dest="command")

    # pulse
    p = sub.add_parser("pulse", help="Project activity summary over a time window")
    p.add_argument("--project", default=None, help="Project key (default: vault JIRA_DEFAULT_PROJECT)")
    p.add_argument("--days", type=int, default=7, help="Look-back window in days (default: 7)")

    # epic-progress
    p = sub.add_parser("epic-progress", help="Deep-dive on all active (non-Done) epics")
    p.add_argument("--project", default=None, help="Project key (default: vault JIRA_DEFAULT_PROJECT)")

    args = parser.parse_args()

    match args.command:
        case "pulse":
            cmd_pulse(args)
        case "epic-progress":
            cmd_epic_progress(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
