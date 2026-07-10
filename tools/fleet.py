#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["redis>=5.0"]
# ///
"""
name: fleet
description: Agent-to-agent comms + orchestration for parallel Claude Code sessions over Redis streams.
categories: [orchestration, fleet, agents, redis, tmux, claude]
secrets:
  - FLEET_REDIS_URL
usage: |
  Overseer side:
    init [--run <id>]                         Mint/select a run id (stored as the shell default)
    spawn <issue> [--base BRANCH] [--run ID] [--prompt FILE]
                  [--session NAME] [--launch CMD] [--no-kickoff] [--dry-run]
                                              Worktree + tmux window + claude + kickoff paste, one shot
    status [<issue>] [--run ID] [--json]      Structured status for all workers (from Redis)
    watch [--timeout N] [--run ID] [--json] [--from ID]
                                              XREAD BLOCK the events stream (Phase 2)
    send <issue> "msg" [--interrupt] [--run ID]
                                              XADD to a worker inbox (Phase 2)
    land <issue...> [--onto BRANCH] [--push]  Assemble + smoke + single-push a batch (Phase 3)
    kill <issue>|--all [--run ID]             Tear down tmux window(s) + worktree(s) (Phase 3)

  Worker side (env FLEET_RUN / FLEET_ISSUE / FLEET_REDIS_URL provided by spawn):
    report --state STATE [--commit SHA] [--msg "..."] [--run ID] [--issue ID]
    ask "question" [--timeout N]              Report needs-decision + block on inbox (Phase 2)
    inbox [--wait] [--timeout N]              Read pending directives (Phase 2)
    stop-hook                                 Claude Code Stop hook: auto-report inferred state

  Config:
    FLEET_REDIS_URL env or vault key (default redis://localhost:6379). Everything is
    namespaced fleet:<run>:... so concurrent runs never collide.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import redis

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
STATE_PATH = Path.home() / ".sherpa" / "fleet.json"
DEFAULT_REDIS_URL = "redis://localhost:6379"

VALID_STATES = {
    "booting",
    "working",
    "blocked",
    "needs-decision",
    "ready",
    "landed",
    "error",
    "idle",
}


# ---------------------------------------------------------------------------
# Config / connection
# ---------------------------------------------------------------------------

def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _redis_url() -> str:
    return (
        os.environ.get("FLEET_REDIS_URL")
        or _load_vault().get("FLEET_REDIS_URL")
        or DEFAULT_REDIS_URL
    )


def _connect() -> redis.Redis:
    url = _redis_url()
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except redis.RedisError as exc:
        print(f"REDIS_ERROR: cannot reach {url}: {exc}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Local shell state (default run id)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def _mint_run_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = os.urandom(2).hex()
    return f"{stamp}-{suffix}"


def _repo_key() -> str:
    """Abs repo root (git), or cwd if not a git repo — the key for the stored run default."""
    try:
        return _git("rev-parse", "--show-toplevel").stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return os.getcwd()


def _store_run(run: str) -> str:
    state = _load_state()
    state.setdefault("repos", {})[_repo_key()] = run
    _save_state(state)
    return run


def _resolve_run(args, *, mint: bool = False) -> str:
    """--run > FLEET_RUN env > stored default (keyed by repo root) > (mint if allowed)."""
    run = getattr(args, "run", None) or os.environ.get("FLEET_RUN")
    if run:
        return run
    stored = _load_state().get("repos", {}).get(_repo_key())
    if stored:
        return stored
    if mint:
        return _store_run(_mint_run_id())
    print(
        "NO_RUN: no run id resolved. Pass --run, set FLEET_RUN, or run `fleet init`.",
        file=sys.stderr,
    )
    sys.exit(1)


def _get_cursor(run: str) -> str | None:
    return _load_state().get("cursors", {}).get(run)


def _set_cursor(run: str, cursor_id: str) -> None:
    state = _load_state()
    state.setdefault("cursors", {})[run] = cursor_id
    _save_state(state)


def _resolve_issue(args) -> str:
    issue = getattr(args, "issue", None) or os.environ.get("FLEET_ISSUE")
    if not issue:
        print("NO_ISSUE: pass --issue or set FLEET_ISSUE.", file=sys.stderr)
        sys.exit(1)
    return str(issue)


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def k_workers(run: str) -> str:
    return f"fleet:{run}:workers"


def k_events(run: str) -> str:
    return f"fleet:{run}:events"


def k_status(run: str, issue: str) -> str:
    return f"fleet:{run}:{issue}:status"


def k_inbox(run: str, issue: str) -> str:
    return f"fleet:{run}:{issue}:inbox"


# ---------------------------------------------------------------------------
# Core write path (concurrency-safe: atomic ops only)
# ---------------------------------------------------------------------------

def _write_status(client: redis.Redis, run: str, issue: str, fields: dict) -> None:
    """HSET individual fields — never read-modify-write a whole blob."""
    payload = {kk: vv for kk, vv in fields.items() if vv is not None}
    payload["updated_at"] = f"{time.time():.3f}"
    client.hset(k_status(run, issue), mapping=payload)


def _emit_event(client: redis.Redis, run: str, issue: str, event: str, fields: dict) -> str:
    entry = {"issue": issue, "event": event, "ts": f"{time.time():.3f}"}
    for kk, vv in fields.items():
        if vv is not None:
            entry[kk] = str(vv)
    return client.xadd(k_events(run), entry)


def _register_worker(client: redis.Redis, run: str, issue: str) -> None:
    client.sadd(k_workers(run), issue)  # atomic


def _last_id(client: redis.Redis, stream: str) -> str:
    """Id of the last entry in a stream, or '0-0' if the stream is empty."""
    tail = client.xrevrange(stream, count=1)
    return tail[0][0] if tail else "0-0"


def _flatten(entries) -> list[dict]:
    """XREAD/XRANGE results → [{id, ...fields}] in arrival order."""
    out = []
    for _stream, items in entries:
        for entry_id, fields in items:
            out.append({"id": entry_id, **fields})
    return out


# ---------------------------------------------------------------------------
# git / tmux helpers
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _repo_root() -> str:
    try:
        return _git("rev-parse", "--show-toplevel").stdout.strip()
    except subprocess.CalledProcessError:
        print("NOT_A_REPO: run from inside a git repository.", file=sys.stderr)
        sys.exit(2)


def _current_branch(cwd: str | None = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True)


def _tmux_available() -> bool:
    try:
        _tmux("has-session", check=False)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    run = _store_run(getattr(args, "run", None) or _mint_run_id())
    print(json.dumps({"run": run, "repo": _repo_key(), "redis_url": _redis_url(), "stored": str(STATE_PATH)}))
    print(f"fleet run: {run}", file=sys.stderr)
    return 0


def _kickoff_prompt(issue: str, run: str, base: str, prompt_file: str | None) -> str:
    if prompt_file:
        return Path(prompt_file).read_text()
    return (
        f"You are a fleet worker for issue {issue} (run {run}, base {base}).\n"
        f"Your worktree is checked out on branch issue-{issue}.\n"
        "Work the issue, committing as you go. When you need a human/overseer decision, run:\n"
        f"  sherpa fleet ask \"<your question>\"\n"
        "When the work is complete and committed, run:\n"
        "  sherpa fleet report --state ready --commit $(git rev-parse HEAD)\n"
        "then STOP. Do not push; the overseer lands the batch."
    )


def _write_worker_settings(worktree: str, run: str, issue: str, redis_url: str) -> Path:
    """Install a Claude Code Stop hook into the worker's project-local settings."""
    settings_dir = Path(worktree) / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        settings = {}

    hook_cmd = "sherpa fleet stop-hook"
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    already = any(
        h.get("command") == hook_cmd
        for entry in stop_hooks
        for h in entry.get("hooks", [])
    )
    if not already:
        stop_hooks.append({"hooks": [{"type": "command", "command": hook_cmd}]})

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path


def cmd_spawn(args) -> int:
    client = _connect()
    run = _resolve_run(args, mint=True)
    issue = str(args.issue)
    redis_url = _redis_url()
    repo = _repo_root()
    base = args.base or _current_branch(cwd=repo)
    branch = f"issue-{issue}"
    worktree = os.path.join(repo, ".claude", "worktrees", f"issue-{issue}")
    window = f"fleet-{issue}"
    session = args.session

    plan = {
        "run": run,
        "issue": issue,
        "base": base,
        "branch": branch,
        "worktree": worktree,
        "window": window,
        "redis_url": redis_url,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}))
        return 0

    # The Stop hook and worker report/ask all shell out to `sherpa`; fail before
    # standing anything up if it can't resolve on PATH.
    if not shutil.which("sherpa"):
        print(
            "SHERPA_NOT_ON_PATH: workers invoke `sherpa fleet ...`; install it "
            "(uv tool install) and ensure it's on PATH before spawning.",
            file=sys.stderr,
        )
        return 2

    # 1. Worktree (idempotent-ish: reuse if the path already exists)
    if not os.path.isdir(worktree):
        try:
            _git("worktree", "add", "-b", branch, worktree, base, cwd=repo)
        except subprocess.CalledProcessError as exc:
            print(f"WORKTREE_ERROR: {exc.stderr.strip()}", file=sys.stderr)
            return 2
    else:
        print(f"worktree exists, reusing: {worktree}", file=sys.stderr)

    # 2. Stop-hook settings overlay
    settings_path = _write_worker_settings(worktree, run, issue, redis_url)

    # 3. Register + seed status (atomic ops)
    _register_worker(client, run, issue)
    _write_status(
        client,
        run,
        issue,
        {
            "state": "booting",
            "branch": branch,
            "base": base,
            "worktree": worktree,
            "window": window,
            "session": session or "",
        },
    )
    _emit_event(client, run, issue, "spawn", {"state": "booting", "branch": branch})

    # 4. tmux window + launch
    if not _tmux_available():
        print("TMUX_UNAVAILABLE: no tmux server; worktree + Redis prepared, launch worker manually.", file=sys.stderr)
        print(json.dumps({"spawned": False, "settings": str(settings_path), **plan}))
        return 0

    env_prefix = (
        f"FLEET_RUN={run} FLEET_ISSUE={issue} FLEET_REDIS_URL={redis_url}"
    )
    launch = args.launch or "claude"
    new_window = ["new-window", "-n", window, "-c", worktree]
    if session:
        new_window += ["-t", session]
    if args.detached:
        new_window.insert(1, "-d")
    _tmux(*new_window)
    target = f"{session}:{window}" if session else window
    _tmux("send-keys", "-t", target, f"{env_prefix} {launch}", "Enter")

    if not args.no_kickoff and launch == "claude":
        prompt = _kickoff_prompt(issue, run, base, args.prompt)
        _paste_kickoff(target, prompt, boot_wait=args.boot_wait)

    print(json.dumps({"spawned": True, "target": target, "settings": str(settings_path), **plan}))
    print(f"spawned worker for issue {issue} in tmux window {window}", file=sys.stderr)
    return 0


def _paste_kickoff(target: str, prompt: str, boot_wait: float = 6.0) -> None:
    """Bracketed paste is REQUIRED — a raw multi-line send submits at the first newline."""
    time.sleep(boot_wait)  # let claude boot its TUI
    _tmux("set-buffer", prompt)
    _tmux("paste-buffer", "-p", "-t", target)  # -p = bracketed paste
    time.sleep(0.4)
    _tmux("send-keys", "-t", target, "Enter")


def _fmt_age(updated_at: str | None) -> str:
    if not updated_at:
        return "-"
    try:
        delta = time.time() - float(updated_at)
    except ValueError:
        return "-"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta / 60)}m"
    return f"{int(delta / 3600)}h"


def cmd_status(args) -> int:
    client = _connect()
    run = _resolve_run(args)
    if args.issue:
        issues = [str(args.issue)]
    else:
        issues = sorted(client.smembers(k_workers(run)))

    rows = []
    for issue in issues:
        h = client.hgetall(k_status(run, issue))
        rows.append(
            {
                "issue": issue,
                "state": h.get("state", "unknown"),
                "commit": (h.get("commit") or "")[:8],
                "branch": h.get("branch", ""),
                "msg": h.get("msg", ""),
                "age": _fmt_age(h.get("updated_at")),
            }
        )

    if args.json:
        print(json.dumps({"run": run, "workers": rows}))
        return 0

    print(f"run: {run}")
    if not rows:
        print("(no workers)")
        return 0
    header = f"{'ISSUE':<8} {'STATE':<14} {'COMMIT':<9} {'AGE':<5} {'BRANCH':<22} MSG"
    print(header)
    print("-" * len(header))
    for r in rows:
        msg = r["msg"] if len(r["msg"]) <= 48 else r["msg"][:45] + "..."
        print(
            f"{r['issue']:<8} {r['state']:<14} {r['commit']:<9} "
            f"{r['age']:<5} {r['branch']:<22} {msg}"
        )
    return 0


def cmd_report(args) -> int:
    if args.state not in VALID_STATES:
        print(
            f"BAD_STATE: {args.state!r} not in {sorted(VALID_STATES)}",
            file=sys.stderr,
        )
        return 1
    client = _connect()
    run = _resolve_run(args)
    issue = _resolve_issue(args)
    _register_worker(client, run, issue)
    _write_status(
        client,
        run,
        issue,
        {"state": args.state, "commit": args.commit, "msg": args.msg},
    )
    event_id = _emit_event(
        client,
        run,
        issue,
        "report",
        {"state": args.state, "commit": args.commit, "msg": args.msg},
    )
    print(json.dumps({"run": run, "issue": issue, "state": args.state, "event_id": event_id}))
    return 0


def _infer_state(client: redis.Redis, run: str, issue: str) -> tuple[str, str | None, str | None]:
    """Infer worker state for the Stop hook: ready if clean tree ahead of base, else working."""
    status = client.hgetall(k_status(run, issue))
    base = status.get("base")
    try:
        commit = _git("rev-parse", "HEAD").stdout.strip()
        dirty = bool(_git("status", "--porcelain").stdout.strip())
    except subprocess.CalledProcessError:
        return "working", None, None

    ahead = False
    if base:
        try:
            count = _git("rev-list", "--count", f"{base}..HEAD").stdout.strip()
            ahead = count.isdigit() and int(count) > 0
        except subprocess.CalledProcessError:
            ahead = False

    state = "ready" if (not dirty and ahead) else "working"
    return state, commit, base


def cmd_stop_hook(args) -> int:
    """Claude Code Stop hook: auto-report inferred state when a worker goes idle.

    Reads FLEET_RUN/FLEET_ISSUE from env (set by spawn). Stdin carries the hook
    JSON payload from Claude Code; we don't need it but drain it so the pipe closes.
    """
    try:
        sys.stdin.read()
    except Exception:
        pass

    run = os.environ.get("FLEET_RUN")
    issue = os.environ.get("FLEET_ISSUE")
    if not run or not issue:
        # Not a fleet worker session — no-op so the hook never blocks a normal claude.
        print(json.dumps({"skipped": "no FLEET_RUN/FLEET_ISSUE"}))
        return 0

    client = _connect()
    # Don't stomp an explicit semantic state the worker just set.
    current = client.hget(k_status(run, issue), "state")
    if current in {"blocked", "needs-decision", "landed", "error"}:
        print(json.dumps({"issue": issue, "state": current, "kept": True}))
        return 0

    state, commit, _base = _infer_state(client, run, issue)
    _register_worker(client, run, issue)
    _write_status(client, run, issue, {"state": state, "commit": commit})
    event_id = _emit_event(client, run, issue, "stop", {"state": state, "commit": commit})
    print(json.dumps({"issue": issue, "state": state, "commit": commit, "event_id": event_id}))
    return 0


def cmd_watch(args) -> int:
    """XREAD BLOCK the events stream. Returns on the first event(s) or timeout.

    Resumes from a per-run cursor (persisted locally) so events between two
    `watch` calls aren't missed; --from overrides ('0' replays the whole stream).
    """
    client = _connect()
    run = _resolve_run(args)
    stream = k_events(run)

    if args.from_id is not None:
        start = "0-0" if args.from_id == "0" else args.from_id
    else:
        start = _get_cursor(run) or "$"

    block_ms = 0 if args.timeout is None else int(args.timeout * 1000)
    try:
        result = client.xread({stream: start}, block=block_ms, count=args.count)
    except redis.RedisError as exc:
        print(f"REDIS_ERROR: {exc}", file=sys.stderr)
        return 2

    events = _flatten(result) if result else []
    if events:
        _set_cursor(run, events[-1]["id"])

    if args.json:
        print(json.dumps({"run": run, "timed_out": not events, "events": events}))
        return 0

    if not events:
        print(f"(timeout, no events) run: {run}")
        return 0
    for e in events:
        commit = (e.get("commit") or "")[:8]
        print(
            f"[{e['id']}] issue {e.get('issue', '?'):<6} "
            f"{e.get('event', '?'):<7} {e.get('state', ''):<14} "
            f"{commit:<9} {e.get('msg', '')}"
        )
    return 0


def _interrupt_worker(client: redis.Redis, run: str, issue: str, msg: str) -> bool:
    """tmux nudge: halt the worker and surface the directive inline (durable copy is in the inbox)."""
    status = client.hgetall(k_status(run, issue))
    window = status.get("window")
    if not window or not _tmux_available():
        return False
    session = status.get("session") or ""
    target = f"{session}:{window}" if session else window
    _tmux("send-keys", "-t", target, "Escape", check=False)
    time.sleep(0.3)
    nudge = f"[fleet] overseer directive (full copy in your inbox — run `sherpa fleet inbox`): {msg}"
    _tmux("set-buffer", nudge, check=False)
    _tmux("paste-buffer", "-p", "-t", target, check=False)
    time.sleep(0.3)
    _tmux("send-keys", "-t", target, "Enter", check=False)
    return True


def cmd_send(args) -> int:
    client = _connect()
    run = _resolve_run(args)
    issue = str(args.issue)
    entry = {"msg": args.message, "from": "overseer", "ts": f"{time.time():.3f}"}
    msg_id = client.xadd(k_inbox(run, issue), entry)
    nudged = False
    if args.interrupt:
        nudged = _interrupt_worker(client, run, issue, args.message)
    print(json.dumps({"run": run, "issue": issue, "msg_id": msg_id, "interrupted": nudged}))
    return 0


def cmd_ask(args) -> int:
    """Worker: report needs-decision, then block on the inbox until the overseer answers."""
    client = _connect()
    run = _resolve_run(args)
    issue = _resolve_issue(args)
    inbox = k_inbox(run, issue)

    # Capture the inbox tail BEFORE announcing, so an answer XADDed the instant we
    # report can't slip in ahead of our read.
    start = _last_id(client, inbox)

    _register_worker(client, run, issue)
    _write_status(client, run, issue, {"state": "needs-decision", "msg": args.question})
    _emit_event(client, run, issue, "ask", {"state": "needs-decision", "msg": args.question})

    block_ms = 0 if args.timeout is None else int(args.timeout * 1000)
    result = client.xread({inbox: start}, block=block_ms, count=1)
    answers = _flatten(result) if result else []
    if not answers:
        print(json.dumps({"run": run, "issue": issue, "answered": False}))
        return 3  # still needs-decision; overseer hasn't answered yet

    answer = answers[0]
    _write_status(client, run, issue, {"state": "working"})
    _emit_event(client, run, issue, "resume", {"state": "working"})
    print(json.dumps({"run": run, "issue": issue, "answered": True, "answer": answer.get("msg", ""), "msg_id": answer["id"]}))
    return 0


def cmd_inbox(args) -> int:
    """Worker: read pending overseer directives (non-blocking, or --wait for the next)."""
    client = _connect()
    run = _resolve_run(args)
    issue = _resolve_issue(args)
    inbox = k_inbox(run, issue)
    cursor_key = f"{inbox}:cursor"

    start = client.get(cursor_key) or "0-0"
    if args.wait:
        block_ms = 0 if args.timeout is None else int(args.timeout * 1000)
        result = client.xread({inbox: start}, block=block_ms, count=args.count)
        messages = _flatten(result) if result else []
    else:
        exclusive = f"({start}" if start != "0-0" else "-"
        raw = client.xrange(inbox, exclusive, "+", count=args.count)
        messages = [{"id": mid, **fields} for mid, fields in raw]

    if messages:
        client.set(cursor_key, messages[-1]["id"])

    if args.json or not messages:
        print(json.dumps({"run": run, "issue": issue, "messages": messages}))
        return 0
    for m in messages:
        print(f"[{m['id']}] {m.get('from', '?')}: {m.get('msg', '')}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet",
        description="Agent-to-agent comms + orchestration for parallel Claude Code sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Mint/select a run id")
    p_init.add_argument("--run", help="Use this run id instead of minting one")
    p_init.set_defaults(func=cmd_init)

    p_spawn = sub.add_parser("spawn", help="Stand up a worker (worktree+tmux+claude+kickoff)")
    p_spawn.add_argument("issue", help="Issue id/number")
    p_spawn.add_argument("--base", help="Base branch (default: current branch)")
    p_spawn.add_argument("--run", help="Run id (default: resolved/minted)")
    p_spawn.add_argument("--prompt", help="File with a custom kickoff prompt")
    p_spawn.add_argument("--session", help="tmux session to create the window in")
    p_spawn.add_argument("--launch", help="Command to launch (default: claude)")
    p_spawn.add_argument("--no-kickoff", action="store_true", help="Skip pasting the kickoff prompt")
    p_spawn.add_argument("--detached", action="store_true", help="Create the tmux window detached (-d)")
    p_spawn.add_argument("--boot-wait", type=float, default=6.0, help="Seconds to wait for claude to boot before paste")
    p_spawn.add_argument("--dry-run", action="store_true", help="Print the plan without doing anything")
    p_spawn.set_defaults(func=cmd_spawn)

    p_status = sub.add_parser("status", help="Structured worker status from Redis")
    p_status.add_argument("issue", nargs="?", help="Limit to one issue")
    p_status.add_argument("--run", help="Run id")
    p_status.add_argument("--json", action="store_true", help="JSON output")
    p_status.set_defaults(func=cmd_status)

    p_report = sub.add_parser("report", help="Worker: report status (uplink)")
    p_report.add_argument("--state", required=True, help=f"One of {sorted(VALID_STATES)}")
    p_report.add_argument("--commit", help="Current commit sha")
    p_report.add_argument("--msg", help="Free-text status message")
    p_report.add_argument("--run", help="Run id (default: FLEET_RUN)")
    p_report.add_argument("--issue", help="Issue id (default: FLEET_ISSUE)")
    p_report.set_defaults(func=cmd_report)

    p_hook = sub.add_parser("stop-hook", help="Claude Code Stop hook: auto-report inferred state")
    p_hook.set_defaults(func=cmd_stop_hook)

    p_watch = sub.add_parser("watch", help="Block on the events stream until a worker speaks")
    p_watch.add_argument("--timeout", type=float, help="Seconds to block (default: block forever)")
    p_watch.add_argument("--run", help="Run id")
    p_watch.add_argument("--from", dest="from_id", help="Start id ('0' replays whole stream; default: resume cursor)")
    p_watch.add_argument("--count", type=int, default=100, help="Max events to return per call")
    p_watch.add_argument("--json", action="store_true", help="JSON output")
    p_watch.set_defaults(func=cmd_watch)

    p_send = sub.add_parser("send", help="XADD a directive to a worker's inbox (downlink)")
    p_send.add_argument("issue", help="Target issue")
    p_send.add_argument("message", help="Directive text")
    p_send.add_argument("--interrupt", action="store_true", help="Also tmux send-keys to nudge the worker now")
    p_send.add_argument("--run", help="Run id")
    p_send.set_defaults(func=cmd_send)

    p_ask = sub.add_parser("ask", help="Worker: report needs-decision + block until answered")
    p_ask.add_argument("question", help="Question for the overseer")
    p_ask.add_argument("--timeout", type=float, help="Seconds to block (default: block forever)")
    p_ask.add_argument("--run", help="Run id (default: FLEET_RUN)")
    p_ask.add_argument("--issue", help="Issue id (default: FLEET_ISSUE)")
    p_ask.set_defaults(func=cmd_ask)

    p_inbox = sub.add_parser("inbox", help="Worker: read pending directives")
    p_inbox.add_argument("--wait", action="store_true", help="Block for the next directive")
    p_inbox.add_argument("--timeout", type=float, help="With --wait: seconds to block")
    p_inbox.add_argument("--count", type=int, default=100, help="Max messages to return")
    p_inbox.add_argument("--run", help="Run id (default: FLEET_RUN)")
    p_inbox.add_argument("--issue", help="Issue id (default: FLEET_ISSUE)")
    p_inbox.add_argument("--json", action="store_true", help="JSON output")
    p_inbox.set_defaults(func=cmd_inbox)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
