#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["python-toon==0.1.3", "httpx", "sqlite-vec"]
# ///
"""
name: corpus
description: Search an indexed local corpus of your Slack — what you promised, what you missed.
categories: [slack, search, memory, knowledge]
axi: true
secrets: [SLACK_USER_TOKEN_GRAVITATE, LITELLM_KEY_CORPUS]
usage: |
  search <QUERY> [--from NAME] [--mine] [--since 30d] [--channel ID] [--k 5] [--context 3]
  with <NAME> [--since 30d] [--limit 50]
  owed <NAME> [--limit 50]
  thread <PERMALINK>
  sync [--workspace NAME] [--budget 900]
  status
operations:
  search:
    tier: read
    argv: ["search", "{query}"]
    optional:
      from_person: "--from"
      mine: "--mine"
      since: "--since"
      workspace: "--workspace"
      channel: "--channel"
      k: "--k"
      context: "--context"
  with:
    tier: read
    argv: ["with", "{person}"]
    optional:
      since: "--since"
      limit: "--limit"
  owed:
    tier: read
    argv: ["owed", "{person}"]
    optional:
      limit: "--limit"
  thread:
    tier: read
    argv: ["thread", "{permalink}"]
  sync:
    tier: write
    argv: ["sync"]
    optional:
      workspace: "--workspace"
      budget: "--budget"
    notes: "Hits the Slack API and writes to the local corpus. Safe to re-run."
  status:
    tier: read
    argv: ["status"]
risk: low
outputBudget: 4000
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

from sherpa.corpus.chunk import rechunk_dirty
from sherpa.corpus.config import ConfigError, MissingSecret, load_config, resolve_token
from sherpa.corpus.db import open_db
from sherpa.corpus.embed import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    EMBED_BUDGET_SHARE,
    EmbeddingError,
    GatewayEmbedder,
    embed_pending,
)
from sherpa.corpus.health import check, is_healthy, warnings
from sherpa.corpus.ingest import ingest_workspace
from sherpa.corpus.search import Filters, Hit, conversation_with, search, sent_to, thread_at
from sherpa.corpus.slack import AuthError, HttpSlackClient, SlackError

RELATIVE = re.compile(r"^(\d+)([hdw])$")
SECONDS = {"h": 3600, "d": 86400, "w": 604800}


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    """Assemble the top-level parser plus one subparser per subcommand.

    `--config`/`--vault`/`--json` are declared on every subparser rather than
    once on the top-level parser: argparse hands everything after the
    subcommand token to that subcommand's own parser, so a flag defined only
    on the parent is invisible to `corpus status --config ...` even though it
    parses fine as `corpus --config ... status`. Every subcommand here is
    always invoked in the former order, so the flags live on each subparser —
    matching the pattern in tools/litellm_keys.py.
    """
    parser = argparse.ArgumentParser(prog="corpus", add_help=True, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command")

    registry = {}
    sync = subparsers.add_parser("sync", allow_abbrev=False)
    sync.add_argument("--workspace", default=None)
    sync.add_argument("--budget", type=float, default=900.0)
    sync.add_argument("--config", default=None)
    sync.add_argument("--vault", default=None)
    sync.add_argument("--json", action="store_true")
    registry["sync"] = sync

    status = subparsers.add_parser("status", allow_abbrev=False)
    status.add_argument("--config", default=None)
    status.add_argument("--json", action="store_true")
    registry["status"] = status

    search_parser = subparsers.add_parser("search", allow_abbrev=False)
    search_parser.add_argument("query")
    search_parser.add_argument("--from", dest="from_person", default=None)
    search_parser.add_argument("--mine", action="store_true")
    search_parser.add_argument("--since", default=None)
    search_parser.add_argument("--workspace", default=None)
    search_parser.add_argument("--channel", default=None)
    search_parser.add_argument("--k", type=int, default=5)
    search_parser.add_argument("--context", type=int, default=3)
    search_parser.add_argument("--config", default=None)
    search_parser.add_argument("--vault", default=None)
    search_parser.add_argument("--json", action="store_true")
    search_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["search"] = search_parser

    with_parser = subparsers.add_parser("with", allow_abbrev=False)
    with_parser.add_argument("person")
    with_parser.add_argument("--since", default=None)
    with_parser.add_argument("--limit", type=int, default=50)
    with_parser.add_argument("--config", default=None)
    with_parser.add_argument("--vault", default=None)
    with_parser.add_argument("--json", action="store_true")
    with_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["with"] = with_parser

    owed_parser = subparsers.add_parser("owed", allow_abbrev=False)
    owed_parser.add_argument("person")
    owed_parser.add_argument("--limit", type=int, default=50)
    owed_parser.add_argument("--config", default=None)
    owed_parser.add_argument("--vault", default=None)
    owed_parser.add_argument("--json", action="store_true")
    owed_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["owed"] = owed_parser

    thread_parser = subparsers.add_parser("thread", allow_abbrev=False)
    thread_parser.add_argument("permalink")
    thread_parser.add_argument("--config", default=None)
    thread_parser.add_argument("--vault", default=None)
    thread_parser.add_argument("--json", action="store_true")
    thread_parser.add_argument("--stale-ok", dest="stale_ok", action="store_true")
    registry["thread"] = thread_parser

    return parser, registry


def sync_lock(db_path):
    """Hold an advisory lock for the duration of a sync, or return None.

    A cron run that overruns its interval meets the next one, and two syncs on
    one SQLite file collide on the write lock -- which surfaced as a raw
    `database is locked` traceback. An overrun is expected operational
    behavior, so the second run steps aside quietly rather than failing.
    """
    import fcntl

    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_suffix(path.suffix + ".sync.lock"), "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def notify(title: str, message: str) -> None:
    """Push loudly. A corpus that stops updating must not do so quietly."""
    try:
        subprocess.run(
            ["sherpa", "notify", "send", message, "--title", title, "--priority", "high"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass  # the non-zero exit and stdout error still carry the failure


def parse_since(value: str) -> float:
    if match := RELATIVE.match(value):
        amount, unit = match.groups()
        return time.time() - int(amount) * SECONDS[unit]
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        fail(
            f"could not read --since {value!r}",
            help="sherpa corpus search <QUERY> --since 30d   (or an ISO date: 2026-01-01)",
            usage=True,
        )


def make_embedder(vault_path: Path | str | None = None):
    vault_path = Path(vault_path) if vault_path else Path.home() / ".sherpa" / "vault.json"
    vault = json.loads(vault_path.read_text()) if vault_path.exists() else {}
    key = vault.get("LITELLM_KEY_CORPUS") or vault.get("LITELLM_KEY", "")
    if not key:
        print("MISSING_SECRET: LITELLM_KEY_CORPUS", file=sys.stderr)
        fail(
            "missing secret LITELLM_KEY_CORPUS",
            help="sherpa vault_manager set LITELLM_KEY_CORPUS <key>   (mint one: sherpa litellm_keys create)",
            usage=True,
        )
    return GatewayEmbedder(DEFAULT_BASE_URL, key, DEFAULT_MODEL)


def render(hits: list[Hit], *, limit_chars: int = 1200) -> list[dict]:
    rendered = []
    for hit in hits:
        transcript, notice = truncate(hit.transcript, limit_chars)
        entry = {
            "when": datetime.fromtimestamp(hit.start_ts).strftime("%Y-%m-%d %H:%M"),
            "where": hit.header,
            "conversation": transcript,
            "permalink": hit.permalink,
        }
        if notice:
            entry["truncated"] = notice
        rendered.append(entry)
    return rendered


def with_health(payload: dict, conn, config, stale_ok: bool) -> dict:
    if stale_ok:
        return payload
    if lines := warnings(check(conn, config)):
        payload["warnings"] = lines
        # Additive, not overwriting: cmd_with/cmd_owed may already have set a
        # pagination `help` hint (see more results with --limit N) before this
        # runs, and both hints are legitimate at once.
        payload["help"] = payload.get("help", []) + ["sherpa corpus sync"]
    return payload


def _require_at_least(value: int, floor: int, flag: str, invocation: str) -> None:
    """Reject an out-of-range numeric flag rather than clamping it.

    Clamping is the wrong failure mode here: `--limit 0` silently turned into
    `--limit 1` would hand back a result the caller did not ask for, and SQLite
    treats a negative `LIMIT` as "no limit at all" -- so a negative value doesn't
    even fail loudly on its own, it silently defeats the cap it was supposed to
    apply. Both look, from the caller's side, exactly like the failure mode this
    project exists to prevent: a confident, wrong answer with nothing to flag it.
    Rejecting means the caller must change the flag before retrying is worth
    attempting, which is exit 2.
    """
    if value < floor:
        fail(
            f"{flag} must be at least {floor} (got {value})",
            help=invocation,
            usage=True,
        )


def cmd_search(args, config) -> int:
    _require_at_least(args.k, 1, "--k", f"sherpa corpus search {args.query!r} --k 5")
    _require_at_least(args.context, 0, "--context", f"sherpa corpus search {args.query!r} --context 3")
    conn = open_db(config.db)
    filters = Filters(
        person=args.from_person,
        mine=args.mine,
        workspace=args.workspace,
        channel=args.channel,
        since=parse_since(args.since) if args.since else None,
    )
    try:
        hits = search(conn, args.query, make_embedder(args.vault), filters=filters, k=args.k, context=args.context)
    except EmbeddingError as error:
        notify("Slack corpus: embedding gateway down", error.detail)
        fail(
            f"embedding gateway unavailable: {error.detail}",
            help="sherpa corpus search <QUERY>   (retry once the gateway is reachable)",
        )

    payload = {"query": args.query, "count": len(hits), "results": render(hits)}
    if not hits:
        payload["results"] = []
        payload["note"] = f"no conversations matched {args.query!r}"
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def _paginated(payload: dict, page, invocation: str) -> dict:
    """Report the true total alongside a possibly-capped page, per
    docs/SHERPA_STANDARDS.md's "report true totals" rule: `count: N of M total`,
    not just the page size, plus a `help` hint naming the invocation that would
    show more when the page was actually capped.
    """
    payload["count"] = len(page.hits)
    payload["total"] = page.total
    payload["results"] = render(page.hits)
    if page.total > len(page.hits):
        payload["help"] = payload.get("help", []) + [invocation]
    return payload


def cmd_with(args, config) -> int:
    _require_at_least(args.limit, 1, "--limit", f'sherpa corpus with "{args.person}" --limit 50')
    conn = open_db(config.db)
    page = conversation_with(
        conn, args.person, since=parse_since(args.since) if args.since else None, limit=args.limit
    )
    payload = _paginated(
        {"person": args.person},
        page,
        f'sherpa corpus with "{args.person}" --limit {max(page.total, args.limit)}',
    )
    if not page.hits:
        payload["note"] = f"no conversations with {args.person!r} in the corpus"
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_owed(args, config) -> int:
    _require_at_least(args.limit, 1, "--limit", f'sherpa corpus owed "{args.person}" --limit 50')
    conn = open_db(config.db)
    page = sent_to(conn, args.person, limit=args.limit)
    payload = _paginated(
        {
            "person": args.person,
            "note": "Read these and summarize what was promised — this is a filter, not a classifier.",
        },
        page,
        f'sherpa corpus owed "{args.person}" --limit {max(page.total, args.limit)}',
    )
    if not page.hits:
        payload["note"] = f"no messages of mine to {args.person!r} in the corpus"
    emit(with_health(payload, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_thread(args, config) -> int:
    conn = open_db(config.db)
    hit = thread_at(conn, args.permalink)
    if hit is None:
        fail(
            f"no message in the corpus at {args.permalink}",
            help="sherpa corpus sync",
        )
    emit(with_health({"result": render([hit])[0]}, conn, config, args.stale_ok), as_json=args.json)
    return 0


def cmd_sync(args, config) -> int:
    lock = sync_lock(config.db)
    if lock is None:
        emit(
            {
                "note": "another sync is already running — this run stepped aside",
                "count": 0,
                "help": ["sherpa corpus status"],
            },
            as_json=args.json,
        )
        return 0

    conn = open_db(config.db)
    selected = [w for w in config.workspaces if args.workspace in (None, w.name)]
    if not selected:
        fail(
            f"no workspace named {args.workspace}",
            help="sherpa corpus status",
            usage=True,
        )

    results = []
    for workspace in selected:
        try:
            token = resolve_token(workspace, args.vault)
        except MissingSecret as missing:
            print(f"MISSING_SECRET: {missing.key}", file=sys.stderr)
            notify("Slack corpus: token missing", f"{workspace.name}: set {missing.key}")
            fail(missing.message, help=missing.help, usage=True)

        client = HttpSlackClient(token)
        # --budget bounds the whole run, not just its first phase: an unbounded
        # embed tail is how a 15-minute cron overlaps itself.
        started = time.time()
        try:
            report = ingest_workspace(
                conn,
                client,
                workspace.name,
                workspace.me,
                budget_seconds=args.budget * (1 - EMBED_BUDGET_SHARE),
            )
        except AuthError as error:
            notify(
                "Slack corpus: auth failed",
                f"{workspace.name}: {error.code} — refresh {workspace.token_key}",
            )
            fail(
                f"{workspace.name}: slack rejected the token ({error.code})",
                help=f"sherpa vault_manager set {workspace.token_key} <xoxp-token>",
                usage=True,
            )
        except SlackError as error:
            # AuthError subclasses SlackError, so this must come after the
            # `except AuthError` above or the auth branch is unreachable.
            # Unlike a bad token, this is the world being uncooperative
            # (rate-limit exhaustion, a repeated pagination cursor, a
            # transient HTTP error) — retrying the same command later is the
            # right response, so this is exit 1, not 2.
            notify(
                "Slack corpus: sync failed",
                f"{workspace.name}: {error.code}",
            )
            fail(
                f"{workspace.name}: slack sync failed ({error.code})",
                help="sherpa corpus sync",
            )

        rechunk_dirty(conn, workspace.name)
        try:
            embedded = embed_pending(
                conn, make_embedder(args.vault), deadline=started + args.budget
            )
        except EmbeddingError as error:
            notify("Slack corpus: embedding gateway down", error.detail)
            fail(
                f"embedding gateway unavailable: {error.detail}",
                help="sherpa corpus sync   (retry once the gateway is reachable)",
            )

        results.append(
            {
                "workspace": report.workspace,
                "messages": report.messages,
                "channels": report.channels_done,
                "failed": len(report.channels_failed),
                "threads_failed": len(report.threads_failed),
                "never_synced": report.channels_never_synced,
                "embedded": embedded,
            }
        )

    failed = [r for r in results if r["failed"] or r["threads_failed"]]
    payload = {"synced": results, "count": len(results)}
    if failed:
        payload["help"] = ["sherpa corpus status"]
    emit(payload, as_json=args.json)
    return 1 if failed else 0


def cmd_status(args, config) -> int:
    conn = open_db(config.db)
    reports = check(conn, config)
    messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]

    payload = {
        "db": str(config.db),
        "messages": messages,
        "chunks": chunks,
        "workspaces": [
            {
                "workspace": r.workspace,
                "last_sync": round(r.last_sync, 0),
                "stale": r.stale,
                "unembedded": r.unembedded,
                "failed": r.failed_channels,
                "never_synced": r.never_synced,
                "backfilling": r.backfilling,
                "unswept_threads": r.unswept_threads,
            }
            for r in reports
        ],
    }
    if lines := warnings(reports):
        payload["warnings"] = lines
        payload["help"] = ["sherpa corpus sync"]
    emit(payload, as_json=args.json)
    return 0 if is_healthy(reports) else 2


def home_view(config_path) -> int:
    print(bin_line(Path(__file__).resolve()))
    print("description: Search an indexed local corpus of your Slack.")
    print("hint: sherpa corpus status")
    print("hint: sherpa corpus search <QUERY>")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser, registry = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit(home_view(None))

    args = parse_strict(parser, registry, argv)
    if not args.command:
        sys.exit(home_view(None))

    try:
        config = load_config(args.config)
    except ConfigError as error:
        fail(error.message, help=error.help, usage=True)

    handlers = {
        "sync": cmd_sync,
        "status": cmd_status,
        "search": cmd_search,
        "with": cmd_with,
        "owed": cmd_owed,
        "thread": cmd_thread,
    }
    sys.exit(handlers[args.command](args, config))


if __name__ == "__main__":
    main()
