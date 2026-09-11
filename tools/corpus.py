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
secrets: [SLACK_USER_TOKEN_GRAVITATE]
usage: |
  search <QUERY> [--from NAME] [--mine] [--since 30d] [--k 5] [--context 3]
  with <NAME> [--since 30d]
  owed <NAME>
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
      k: "--k"
      context: "--context"
  with:
    tier: read
    argv: ["with", "{person}"]
    optional:
      since: "--since"
  owed:
    tier: read
    argv: ["owed", "{person}"]
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
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

from sherpa.corpus.config import ConfigError, MissingSecret, load_config, resolve_token
from sherpa.corpus.db import open_db
from sherpa.corpus.health import check, is_healthy, warnings
from sherpa.corpus.ingest import ingest_workspace
from sherpa.corpus.slack import AuthError, HttpSlackClient, SlackError


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

    for name, positional in (
        ("search", "query"),
        ("with", "person"),
        ("owed", "person"),
        ("thread", "permalink"),
    ):
        stub = subparsers.add_parser(name, allow_abbrev=False)
        stub.add_argument(positional)
        stub.add_argument("--config", default=None)
        stub.add_argument("--json", action="store_true")
        registry[name] = stub

    return parser, registry


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


def cmd_sync(args, config) -> int:
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
        try:
            report = ingest_workspace(
                conn, client, workspace.name, workspace.me, budget_seconds=args.budget
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
        results.append(
            {
                "workspace": report.workspace,
                "messages": report.messages,
                "channels": report.channels_done,
                "failed": len(report.channels_failed),
            }
        )

    failed = [r for r in results if r["failed"]]
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
            }
            for r in reports
        ],
    }
    if lines := warnings(reports):
        payload["warnings"] = lines
        payload["help"] = ["sherpa corpus sync"]
    emit(payload, as_json=args.json)
    return 0 if is_healthy(reports) else 2


def cmd_unimplemented(args, config) -> int:
    """Fail as `usage=True`: retrying identically can never work until Task 10 ships."""
    fail(
        f"'{args.command}' is not implemented yet",
        help="sherpa corpus status",
        usage=True,
    )


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
        "search": cmd_unimplemented,
        "with": cmd_unimplemented,
        "owed": cmd_unimplemented,
        "thread": cmd_unimplemented,
    }
    sys.exit(handlers[args.command](args, config))


if __name__ == "__main__":
    main()
