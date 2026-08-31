#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
# A raw docstring: the notes below mention \n and \t as literal escape
# sequences, and in a cooked string Python turns them into real newlines
# before any YAML parser sees them — which ends the block scalar early and
# makes the whole header unparseable to `ast.get_docstring` (the indexer
# path), so this tool was silently missing from the MCP index. The CLI's
# regex reads the source text and never saw it.
r"""
name: slack_messenger
description: Send Slack messages to channels and DMs. Supports @(name) for user mentions and auto-links Jira keys and GitHub PRs. Fuzzy user/channel lookup with local caching.
categories: [slack, messaging, communication]
secrets:
  - SLACK_USER_TOKEN
usage: |
  send --channel <name> --text 'Hello team!'
  send --channel-id C01ABC23DEF --file /tmp/msg.txt
  send --channel general --stdin < message.txt
  send --channel general --text 'Hey @(jane doe) check this out'
  send --channel general --text 'reply' --thread 1774551827.458609
  send --channel general --blocks /tmp/blocks.json --text 'fallback'
  send --channel general --attach ./SKILL.md --text 'here is the skill'
  send --channel general --attach ./a.png --attach ./b.png --title 'Screens'
  dm --user <name> --text 'Hey, quick question...'
  dm --user-id U01ABC23DEF --file /tmp/msg.txt
  dm --user wesley --attach ./report.md --title 'Weekly report'
  channels [--filter general] [--refresh]
  users [--filter swap] [--refresh]
notes: |
  @(name) in message text becomes a Slack @mention. Errors if ambiguous (e.g. multiple "nathan"s).
  Jira ticket keys (e.g. KB-123) are auto-linked. Channel/user lookups are cached locally.
  GitHub PRs are auto-linked too: paste the PR URL or write the shorthand owner/repo#123,
  and it renders as #123 (or owner/repo#123 when the message spans several repos).
  --text interprets \n, \t and \r as real characters (\\ sends a literal backslash);
  --file/--stdin are taken verbatim, so code snippets keep their backslashes.
  --file/--stdin supply the message BODY; --attach uploads a real file (repeatable).
  With --attach the message text becomes the upload's initial comment.
  Text over 4000 chars is uploaded as a file attachment rather than truncated by Slack;
  pass --no-upload-fallback to send it as-is instead.
risk: high
operations:
  channels:
    tier: read
    argv: ["channels"]
    optional:
      filter: "--filter"
      refresh: "--refresh"
  users:
    tier: read
    argv: ["users"]
    optional:
      filter: "--filter"
      refresh: "--refresh"
  send:
    tier: write
    argv: ["send", "--channel", "{channel}", "--text", "{text}"]
    optional:
      thread: "--thread"
    notes: "Posts publicly and cannot be unsent. @(name) in text becomes a mention."
  dm:
    tier: write
    argv: ["dm", "--user", "{user}", "--text", "{text}"]
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
CACHE_DIR = Path.home() / ".sherpa" / "cache"
SLACK_ID_RE = re.compile(r"^[CGUWDBT][A-Z0-9]{8,}$")
JIRA_KEY_RE = r"\b(?P<jira>[A-Z][A-Z0-9]+-\d+)\b"
GITHUB_REPO_RE = r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
# Slack links and bare URLs are the segments a reference must never be rewritten inside.
LINK_TOKEN_RE = re.compile(r"(<[^>]+>|https?://\S+)")
# Deeper paths (/files, /commits) are left alone: only the PR itself gets a label.
GITHUB_PR_URL_RE = re.compile(
    rf"(?P<url>https://github\.com/{GITHUB_REPO_RE}/pull/(?P<pr>\d+))/?(?![\w/])"
)
PLAIN_REF_RE = re.compile(
    rf"(?<![\w./-]){GITHUB_REPO_RE}#(?P<pr>\d+)\b|{JIRA_KEY_RE}"
)
MENTION_RE = re.compile(r"@\(([^)]+)\)")
RATE_LIMIT_THRESHOLD = 30  # seconds — auto-retry if Retry-After <= this
SLACK_TEXT_LIMIT = 4000  # chars; Slack truncates chat.postMessage text beyond this
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _linkify_refs(text: str) -> str:
    """Replace Jira keys and GitHub pull-request references with Slack links.

    Jira keys (KB-12345), bare PR URLs and the ``owner/repo#123`` shorthand all
    become ``<url|label>``. PR labels are bare ``#123`` unless the message spans
    more than one repository, in which case every label carries its repo.
    Anything already inside a Slack ``<...>`` link is left untouched, so running
    this twice is a no-op.
    """
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    jira_url = vault.get("JIRA_URL", "").rstrip("/")

    tokens = [t for t in LINK_TOKEN_RE.split(text) if t]
    repos = _referenced_repos(tokens)
    label = _pr_labeller(qualified=len(repos) > 1)

    for i, token in enumerate(tokens):
        if LINK_TOKEN_RE.fullmatch(token):
            tokens[i] = _link_bare_pr_url(token, label)
        else:
            tokens[i] = PLAIN_REF_RE.sub(lambda m: _plain_ref_link(m, jira_url, label), token)
    return "".join(tokens)


def _referenced_repos(tokens: list[str]) -> set[tuple[str, str]]:
    """Every owner/repo a PR reference in these tokens points at."""
    repos = set()
    for token in tokens:
        if LINK_TOKEN_RE.fullmatch(token):
            if match := GITHUB_PR_URL_RE.match(token):
                repos.add((match["owner"], match["repo"]))
        else:
            repos.update(
                (m["owner"], m["repo"]) for m in PLAIN_REF_RE.finditer(token) if m["pr"]
            )
    return repos


def _pr_labeller(qualified: bool):
    def label(owner: str, repo: str, number: str) -> str:
        return f"{owner}/{repo}#{number}" if qualified else f"#{number}"

    return label


def _link_bare_pr_url(url: str, label) -> str:
    match = GITHUB_PR_URL_RE.match(url)
    if not match:
        return url
    trailing = url[match.end():]
    return f"<{match['url']}|{label(match['owner'], match['repo'], match['pr'])}>{trailing}"


def _plain_ref_link(match: re.Match, jira_url: str, label) -> str:
    if match["pr"]:
        owner, repo, number = match["owner"], match["repo"], match["pr"]
        url = f"https://github.com/{owner}/{repo}/pull/{number}"
        return f"<{url}|{label(owner, repo, number)}>"
    if not jira_url:
        return match[0]
    return f"<{jira_url}/browse/{match['jira']}|{match['jira']}>"


# --- Cache helpers ---


def _load_cache(name: str) -> list[dict]:
    path = CACHE_DIR / f"slack_{name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_cache(name: str, data: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"slack_{name}.json").write_text(json.dumps(data))


def _invalidate_cache(name: str) -> None:
    path = CACHE_DIR / f"slack_{name}.json"
    if path.exists():
        path.unlink()


# --- Slack API helpers ---


def _handle_rate_limit(resp: httpx.Response) -> None:
    """Check for rate limiting and either sleep or bail with a message."""
    data = resp.json()
    if data.get("ok") or data.get("error") != "ratelimited":
        return
    retry_after = int(resp.headers.get("Retry-After", "60"))
    if retry_after <= RATE_LIMIT_THRESHOLD:
        print(f"Rate limited, retrying in {retry_after}s...", file=sys.stderr)
        raise _RateLimitRetry(retry_after)
    print(f"Rate limited by Slack. Try again in {retry_after} seconds.", file=sys.stderr)
    print(f"RETRY_AFTER:{retry_after}", file=sys.stderr)
    sys.exit(2)


class _RateLimitRetry(Exception):
    def __init__(self, wait: int):
        self.wait = wait


async def _slack_get(client: httpx.AsyncClient, headers: dict, url: str, params: dict | None = None) -> dict:
    while True:
        resp = await client.get(url, headers=headers, params=params or {})
        try:
            _handle_rate_limit(resp)
        except _RateLimitRetry as e:
            await asyncio.sleep(e.wait)
            continue
        data = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            print(f"Slack API error: {error}", file=sys.stderr)
            sys.exit(2)
        return data


async def _slack_post(client: httpx.AsyncClient, headers: dict, url: str, payload: dict) -> dict:
    while True:
        resp = await client.post(url, headers=headers, json=payload)
        try:
            _handle_rate_limit(resp)
        except _RateLimitRetry as e:
            await asyncio.sleep(e.wait)
            continue
        data = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            print(f"Slack API error: {error}", file=sys.stderr)
            sys.exit(2)
        return data


# --- Fetch-all helpers (populate cache) ---


async def _fetch_all_channels(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    results = []
    cursor = None
    while True:
        params = {"types": "public_channel,private_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/conversations.list", params)
        for ch in data.get("channels", []):
            results.append({
                "id": ch["id"],
                "name": ch.get("name"),
                "num_members": ch.get("num_members", 0),
            })
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache("channels", results)
    return results


async def _fetch_all_users(client: httpx.AsyncClient, headers: dict) -> list[dict]:
    results = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = await _slack_get(client, headers, "https://slack.com/api/users.list", params)
        for user in data.get("members", []):
            if user.get("deleted") or user.get("is_bot"):
                continue
            results.append({
                "id": user["id"],
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "display_name": user.get("profile", {}).get("display_name"),
            })
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _save_cache("users", results)
    return results


# --- Resolve helpers (cache-first, invalidate on miss) ---


def _search_channels(channels: list[dict], query: str) -> dict | None:
    query_lower = query.lstrip("#").lower()
    for ch in channels:
        if query_lower in ch.get("name", "").lower():
            return ch
    return None


def _search_users(users: list[dict], query: str) -> dict | None:
    query_lower = query.lower()
    for user in users:
        fields = [
            user.get("name", ""),
            user.get("real_name", ""),
            user.get("display_name", ""),
        ]
        if any(query_lower in f.lower() for f in fields):
            return user
    return None


def _find_user_matches(users: list[dict], query: str) -> list[dict]:
    """Find all users matching a query substring. Returns all matches."""
    query_lower = query.lower()
    matches = []
    for user in users:
        fields = [
            user.get("name", ""),
            user.get("real_name", ""),
            user.get("display_name", ""),
        ]
        if any(query_lower in f.lower() for f in fields if f):
            matches.append(user)
    return matches


async def _resolve_mention(client: httpx.AsyncClient, headers: dict, query: str) -> str:
    """Resolve a @(name) mention to a Slack user ID. Errors on ambiguity."""
    # Try cache first, fetch only if no cache exists
    users = _load_cache("users")
    if not users:
        users = await _fetch_all_users(client, headers)

    matches = _find_user_matches(users, query)

    if not matches:
        print(f"User not found for mention: @({query})", file=sys.stderr)
        sys.exit(2)

    if len(matches) == 1:
        return f"<@{matches[0]['id']}>"

    # Multiple matches — check for an exact full-name match
    query_lower = query.lower()
    exact = [u for u in matches if
             (u.get("real_name", "") or "").lower() == query_lower or
             (u.get("display_name", "") or "").lower() == query_lower or
             (u.get("name", "") or "").lower() == query_lower]
    if len(exact) == 1:
        return f"<@{exact[0]['id']}>"

    names = [u.get("real_name") or u.get("display_name") or u.get("name") for u in matches]
    print(f"Ambiguous mention @({query}) — matches: {', '.join(names)}. Be more specific.", file=sys.stderr)
    sys.exit(2)


async def _linkify_mentions(client: httpx.AsyncClient, headers: dict, text: str) -> str:
    """Replace @(name) patterns with Slack <@USER_ID> mentions."""
    mentions = MENTION_RE.findall(text)
    if not mentions:
        return text
    for name in mentions:
        slack_mention = await _resolve_mention(client, headers, name)
        text = text.replace(f"@({name})", slack_mention)
    return text


async def _channel_info(client: httpx.AsyncClient, headers: dict, channel_id: str) -> dict:
    """Look up a single channel by ID — 1 API call instead of paginating all channels."""
    data = await _slack_get(client, headers, "https://slack.com/api/conversations.info", {"channel": channel_id})
    ch = data.get("channel", {})
    return {"id": ch["id"], "name": ch.get("name", channel_id)}


async def _resolve_channel(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    if SLACK_ID_RE.match(query):
        return await _channel_info(client, headers, query)

    # Try cache first
    cached = _load_cache("channels")
    if cached:
        match = _search_channels(cached, query)
        if match:
            return match

    # No cache at all — do initial fetch
    if not cached:
        fresh = await _fetch_all_channels(client, headers)
        match = _search_channels(fresh, query)
        if match:
            return match

    # Channel not found — give actionable advice
    print(f"Channel not found: {query}", file=sys.stderr)
    print("Hint: use --channel-id <ID> to skip name lookup, or run 'channels --refresh' to rebuild the cache.", file=sys.stderr)
    sys.exit(2)


async def _resolve_user(client: httpx.AsyncClient, headers: dict, query: str) -> dict:
    if SLACK_ID_RE.match(query):
        return {"id": query, "name": query}

    # Try cache first
    cached = _load_cache("users")
    if cached:
        match = _search_users(cached, query)
        if match:
            return match

    # No cache at all — do initial fetch
    if not cached:
        fresh = await _fetch_all_users(client, headers)
        match = _search_users(fresh, query)
        if match:
            return match

    print(f"User not found: {query}", file=sys.stderr)
    print("Hint: use --user-id <ID> to skip name lookup, or run 'users --refresh' to rebuild the cache.", file=sys.stderr)
    sys.exit(2)


async def _open_dm(client: httpx.AsyncClient, headers: dict, user_id: str) -> str:
    """Open a DM conversation and return the channel ID."""
    data = await _slack_post(client, headers, "https://slack.com/api/conversations.open", {"users": user_id})
    return data["channel"]["id"]


# --- File uploads ---


async def _upload_one(client: httpx.AsyncClient, headers: dict, path: Path, title: str | None) -> dict:
    """Reserve an upload URL, PUT the bytes, and return the completeUpload descriptor."""
    content = path.read_bytes()
    if not content:
        print(f"Cannot upload an empty file: {path}", file=sys.stderr)
        sys.exit(2)

    reservation = await _slack_get(
        client,
        headers,
        "https://slack.com/api/files.getUploadURLExternal",
        {"filename": path.name, "length": len(content)},
    )
    resp = await client.post(
        reservation["upload_url"],
        headers=headers,
        files={"file": (path.name, content)},
    )
    if resp.status_code >= 400:
        print(f"Upload of {path.name} failed: HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(2)
    return {"id": reservation["file_id"], "title": title or path.name}


async def _upload_files(
    client: httpx.AsyncClient,
    headers: dict,
    channel_id: str,
    paths: list[Path],
    title: str | None,
    initial_comment: str | None,
    thread_ts: str | None,
) -> dict:
    files = [await _upload_one(client, headers, p, title) for p in paths]
    payload: dict = {"files": files, "channel_id": channel_id}
    if initial_comment:
        payload["initial_comment"] = initial_comment
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return await _slack_post(client, headers, "https://slack.com/api/files.completeUploadExternal", payload)


def _resolve_attachments(args: argparse.Namespace) -> list[Path]:
    paths = [Path(a).expanduser() for a in (getattr(args, "attach", None) or [])]
    for path in paths:
        if not path.is_file():
            print(f"Attachment not found: {path}", file=sys.stderr)
            sys.exit(1)
    return paths


def _spill_to_file(text: str) -> Path:
    """Write over-length message text to a temp file so it can be uploaded intact."""
    path = Path(tempfile.mkdtemp(prefix="slack_messenger_")) / "message.md"
    path.write_text(text)
    return path


# --- Text resolution ---


def _unescape(text: str) -> str:
    r"""Turn the escape sequences a caller can only type as text into real characters.

    Argv carries no line breaks, so `--text 'a\nb'` arrives as a literal backslash-n and
    Slack would render it that way. `\\` yields one backslash, and any other escape is
    left alone so regexes and Windows paths survive.
    """
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(0)), text)


def _resolve_text(args: argparse.Namespace, *, required: bool = True) -> str:
    """Resolve message text from --text, --file, or --stdin."""
    if getattr(args, "file", None):
        return Path(args.file).read_text().strip()
    if getattr(args, "stdin", False):
        return sys.stdin.read().strip()
    if getattr(args, "text", None):
        return _unescape(args.text)
    if not required:
        return ""
    print("One of --text, --file, or --stdin is required", file=sys.stderr)
    sys.exit(1)


# --- Delivery ---


async def _deliver(client: httpx.AsyncClient, headers: dict, channel_id: str, args: argparse.Namespace) -> dict:
    """Post a message, or upload attachments with the message as their initial comment."""
    attachments = _resolve_attachments(args)
    if attachments and args.blocks:
        print("--blocks cannot be combined with --attach", file=sys.stderr)
        sys.exit(1)

    raw_text = _resolve_text(args, required=not attachments)
    text = await _linkify_mentions(client, headers, _linkify_refs(raw_text)) if raw_text else ""

    if not attachments and len(text) > SLACK_TEXT_LIMIT and not args.no_upload_fallback:
        print(
            f"Message is {len(text)} chars, over Slack's {SLACK_TEXT_LIMIT}-char limit — "
            "uploading it as a file instead of letting Slack truncate it. "
            "Pass --no-upload-fallback to send it as text anyway.",
            file=sys.stderr,
        )
        attachments = [_spill_to_file(raw_text)]
        text = ""

    if attachments:
        data = await _upload_files(
            client, headers, channel_id, attachments, args.title, text or None, args.thread
        )
        return {
            "ts": next((s.get("ts") for f in data.get("files", []) for s in _shares(f)), None),
            "files": [{"id": f.get("id"), "permalink": f.get("permalink")} for f in data.get("files", [])],
        }

    payload = {"channel": channel_id, "text": text}
    if args.thread:
        payload["thread_ts"] = args.thread
    if args.blocks:
        payload["blocks"] = json.loads(Path(args.blocks).read_text())
    data = await _slack_post(client, headers, "https://slack.com/api/chat.postMessage", payload)
    return {"ts": data.get("ts")}


def _shares(file_info: dict) -> list[dict]:
    shares = file_info.get("shares", {})
    return [s for scope in shares.values() for entries in scope.values() for s in entries]


# --- Subcommands ---


async def _cmd_send(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        if args.channel_id:
            channel = await _channel_info(client, headers, args.channel_id)
        else:
            channel = await _resolve_channel(client, headers, args.channel)
        result = await _deliver(client, headers, channel["id"], args)
        print(json.dumps({
            "ok": True,
            "channel": channel.get("name", channel["id"]),
            **result,
        }))


async def _cmd_dm(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        if args.user_id:
            user = {"id": args.user_id, "name": args.user_id}
        else:
            user = await _resolve_user(client, headers, args.user)
        dm_channel_id = await _open_dm(client, headers, user["id"])
        result = await _deliver(client, headers, dm_channel_id, args)
        print(json.dumps({
            "ok": True,
            "user": user.get("real_name", user.get("name", user["id"])),
            **result,
        }))


async def _cmd_channels(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None

    # Use cache if available and filtering; always refresh on --refresh
    if args.refresh:
        _invalidate_cache("channels")

    cached = _load_cache("channels")
    if cached and not args.refresh:
        results = cached
    else:
        async with httpx.AsyncClient() as client:
            results = await _fetch_all_channels(client, headers)

    if filter_lower:
        results = [ch for ch in results if filter_lower in ch.get("name", "").lower()]
    print(json.dumps(results))


async def _cmd_users(args: argparse.Namespace) -> None:
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    filter_lower = args.filter.lower() if args.filter else None

    if args.refresh:
        _invalidate_cache("users")

    cached = _load_cache("users")
    if cached and not args.refresh:
        results = cached
    else:
        async with httpx.AsyncClient() as client:
            results = await _fetch_all_users(client, headers)

    if filter_lower:
        results = [u for u in results if any(
            filter_lower in (u.get(f, "") or "").lower()
            for f in ("name", "real_name", "display_name")
        )]
    print(json.dumps(results))


# --- CLI ---


def _add_attach_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--attach", action="append", metavar="PATH",
                     help="Upload a real file (repeatable); message text becomes its initial comment")
    sub.add_argument("--title", default=None, help="Title for the uploaded file(s)")
    sub.add_argument("--no-upload-fallback", action="store_true",
                     help="Send over-length text as-is instead of uploading it as a file")


def main():
    parser = argparse.ArgumentParser(description="Send Slack messages to channels and DMs.")
    subparsers = parser.add_subparsers(dest="command")

    send_parser = subparsers.add_parser("send", help="Post a message to a channel")
    send_ch = send_parser.add_mutually_exclusive_group(required=True)
    send_ch.add_argument("--channel", help="Channel name (resolved via lookup)")
    send_ch.add_argument("--channel-id", help="Channel ID (skips name resolution)")
    send_text = send_parser.add_mutually_exclusive_group()
    send_text.add_argument("--text", help="Message text")
    send_text.add_argument("--file", help="Read message text from a file")
    send_text.add_argument("--stdin", action="store_true", help="Read message text from stdin")
    send_parser.add_argument("--thread", default=None, help="Thread timestamp to reply to")
    send_parser.add_argument("--blocks", default=None, help="Path to Block Kit JSON file")
    _add_attach_args(send_parser)

    dm_parser = subparsers.add_parser("dm", help="Send a direct message to a user")
    dm_user = dm_parser.add_mutually_exclusive_group(required=True)
    dm_user.add_argument("--user", help="Username or display name (resolved via lookup)")
    dm_user.add_argument("--user-id", help="User ID (skips name resolution)")
    dm_text = dm_parser.add_mutually_exclusive_group()
    dm_text.add_argument("--text", help="Message text")
    dm_text.add_argument("--file", help="Read message text from a file")
    dm_text.add_argument("--stdin", action="store_true", help="Read message text from stdin")
    dm_parser.add_argument("--thread", default=None, help="Thread timestamp to reply to")
    dm_parser.add_argument("--blocks", default=None, help="Path to Block Kit JSON file")
    _add_attach_args(dm_parser)

    channels_parser = subparsers.add_parser("channels", help="List channels")
    channels_parser.add_argument("--filter", default=None, help="Substring filter on channel name")
    channels_parser.add_argument("--refresh", action="store_true", help="Force refresh from Slack API")

    users_parser = subparsers.add_parser("users", help="List/search users")
    users_parser.add_argument("--filter", default=None, help="Substring filter on name/display_name")
    users_parser.add_argument("--refresh", action="store_true", help="Force refresh from Slack API")

    args = parser.parse_args()

    match args.command:
        case "send":
            asyncio.run(_cmd_send(args))
        case "dm":
            asyncio.run(_cmd_dm(args))
        case "channels":
            asyncio.run(_cmd_channels(args))
        case "users":
            asyncio.run(_cmd_users(args))
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
