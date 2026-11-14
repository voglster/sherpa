#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3", "httpx"]
# ///
"""
name: piper
description: Wake Piper (Jim's Grok Bot assistant) with a short status note — task done, blocked, error, or fyi — via its routine webhook.
categories: [notification, grok, piper, agents, handoff]
axi: true
secrets:
  - PIPER_WEBHOOK_URL
  - PIPER_WEBHOOK_AUTH
usage: |
  piper "<one-line summary>" [--session NAME] [--host HOST] [--kind done|blocked|error|fyi] [--json]
  Posts {source, host, session, kind, message, ts} to Piper's webhook. Never blocks long:
  a failed post warns on stderr and still exits 0.
  Vault keys (paste from the Grok Bot routine's Webhook URL and Authorization header fields):
    PIPER_WEBHOOK_URL    the routine's webhook URL
    PIPER_WEBHOOK_AUTH   the full Authorization header value
"""

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import emit, fail, parse_strict

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
SECRET_KEYS = ("PIPER_WEBHOOK_URL", "PIPER_WEBHOOK_AUTH")
KINDS = ("done", "blocked", "error", "fyi")
SOURCE = "sherpa"
POST_TIMEOUT_SECONDS = 5.0
MAX_MESSAGE_CHARS = 500


def load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def require_secrets(vault: dict) -> tuple[str, str]:
    missing = [key for key in SECRET_KEYS if not vault.get(key)]
    if missing:
        for key in missing:
            print(f"MISSING_SECRET: {key}", file=sys.stderr)
        fail(
            f"missing vault key(s) {', '.join(missing)} — copy them from the Grok Bot app's "
            "Lumbergh overseer routine (Webhook URL and Authorization header fields)",
            help=" && ".join(f"sherpa vault_manager set {key} '<value>'" for key in missing),
            usage=True,
        )
    return vault["PIPER_WEBHOOK_URL"], vault["PIPER_WEBHOOK_AUTH"]


def current_tmux_session() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def clip(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return message[: MAX_MESSAGE_CHARS - 1] + "…"


def build_payload(message: str, *, kind: str, session: str | None, host: str | None) -> dict:
    return {
        "source": SOURCE,
        "host": host or socket.gethostname(),
        "session": session if session is not None else current_tmux_session(),
        "kind": kind,
        "message": clip(message),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def post(url: str, auth: str, payload: dict) -> int | None:
    """Return the HTTP status, or None if the webhook could not be reached.

    httpx exception text embeds the request URL, so only the exception type
    is ever surfaced — the URL is a secret.
    """
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": auth},
            timeout=POST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        print(f"warning: piper webhook unreachable ({type(error).__name__})", file=sys.stderr)
        return None
    if not response.is_success:
        print(f"warning: piper webhook answered HTTP {response.status_code}", file=sys.stderr)
    return response.status_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sherpa piper", description="Wake Piper with a short status note.")
    parser.add_argument("message", nargs="?", help="One-line, factual summary (no secrets, no diffs)")
    parser.add_argument("--session", default=None, help="Session name (default: current tmux session)")
    parser.add_argument("--host", default=None, help="Host name (default: this machine)")
    parser.add_argument("--kind", choices=KINDS, default="fyi", help="done | blocked | error | fyi")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of TOON")
    args = parse_strict(parser, argv=argv)

    if not args.message or not args.message.strip():
        fail("a message is required", help='sherpa piper "<one-line summary>" --kind done', usage=True)

    url, auth = require_secrets(load_vault())
    payload = build_payload(args.message, kind=args.kind, session=args.session, host=args.host)
    status = post(url, auth, payload)
    emit(
        {
            "sent": status is not None and 200 <= status < 300,
            "status": status,
            "kind": payload["kind"],
            "session": payload["session"],
        },
        as_json=args.json,
    )


if __name__ == "__main__":
    main()
