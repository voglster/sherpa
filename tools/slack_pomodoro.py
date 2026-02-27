#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
name: slack_pomodoro
description: Run a Pomodoro timer that manages Slack status, presence, and DND automatically.
categories: [slack, productivity, pomodoro, focus]
secrets:
  - SLACK_USER_TOKEN
usage: |
  start [--work 25] [--break 5] [--status 'Custom status text']
  stop
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
STATE_PATH = Path.home() / ".sherpa" / "pomodoro.json"


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _read_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _remove_state() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def _daemon_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --- Slack API helpers ---

async def _set_presence(headers: dict, away: bool = True) -> None:
    presence = "away" if away else "auto"
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/users.setPresence",
            headers=headers,
            data={"presence": presence},
        )


async def _set_dnd(headers: dict, minutes: int | None = None) -> None:
    if minutes:
        url = "https://slack.com/api/dnd.setSnooze"
        data = {"num_minutes": minutes}
    else:
        url = "https://slack.com/api/dnd.endSnooze"
        data = {}
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, data=data)


async def _update_status(headers: dict, status_text: str, status_emoji: str, expiration: int = 0) -> None:
    profile = {
        "status_text": status_text,
        "status_emoji": status_emoji,
        "status_expiration": expiration,
    }
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/users.profile.set",
            headers=headers,
            json={"profile": profile},
        )


# --- Subcommands ---

def cmd_start(args: argparse.Namespace) -> None:
    state = _read_state()
    if state and _daemon_alive(state.get("pid", -1)):
        print("A pomodoro session is already active. Cancel it first.", file=sys.stderr)
        print(json.dumps({"error": "session_active"}))
        sys.exit(1)

    # Clean up stale state file if daemon is dead
    if state:
        _remove_state()

    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}

    # Set initial Slack state (work phase)
    now = time.time()
    work_end = now + args.work * 60
    status_text = args.status or "In Pomodoro session"
    asyncio.run(_start_work_phase(headers, args.work, work_end, status_text))

    # Write state file before spawning daemon so it's available immediately
    state = {
        "phase": "work",
        "work_minutes": args.work,
        "break_minutes": args.brk,
        "phase_start": now,
        "phase_end": work_end,
        "status_text": status_text,
        "pid": None,  # filled in after spawn
    }
    _write_state(state)

    # Spawn background daemon
    proc = subprocess.Popen(
        [sys.executable, __file__, "_daemon"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Update state with daemon PID
    state["pid"] = proc.pid
    _write_state(state)

    print(json.dumps({
        "status": "started",
        "phase": "work",
        "work_minutes": args.work,
        "break_minutes": args.brk,
        "phase_end": work_end,
        "pid": proc.pid,
    }))


async def _start_work_phase(headers: dict, work_minutes: int, work_end: float, status_text: str = "In Pomodoro session") -> None:
    await _set_presence(headers, away=True)
    await _set_dnd(headers, minutes=work_minutes)
    await _update_status(headers, status_text, ":tomato:", int(work_end))


def cmd_status(args: argparse.Namespace) -> None:
    state = _read_state()
    if not state:
        print(json.dumps({"status": "inactive"}))
        return

    pid = state.get("pid", -1)
    alive = _daemon_alive(pid) if pid and pid > 0 else False
    now = time.time()
    remaining = max(0, state["phase_end"] - now)
    mins, secs = divmod(int(remaining), 60)

    print(json.dumps({
        "status": "active",
        "phase": state["phase"],
        "time_remaining": f"{mins:02d}:{secs:02d}",
        "remaining_seconds": int(remaining),
        "work_minutes": state["work_minutes"],
        "break_minutes": state["break_minutes"],
        "daemon_alive": alive,
        "pid": pid,
    }))


def cmd_cancel(args: argparse.Namespace) -> None:
    state = _read_state()
    if not state:
        print(json.dumps({"status": "inactive", "message": "No active session"}))
        return

    # Kill daemon if alive
    pid = state.get("pid", -1)
    if pid and pid > 0 and _daemon_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Reset Slack state
    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    asyncio.run(_reset_slack(headers))

    _remove_state()
    print(json.dumps({"status": "cancelled"}))


async def _reset_slack(headers: dict) -> None:
    await _set_dnd(headers)  # end snooze
    await _set_presence(headers, away=False)
    await _update_status(headers, "", "")


# --- Background daemon ---

def cmd_daemon() -> None:
    """Background daemon that manages phase transitions and cleanup."""
    try:
        _run_daemon()
    except Exception:
        _remove_state()
        sys.exit(2)


def _run_daemon() -> None:
    state = _read_state()
    if not state:
        sys.exit(0)

    token = _load_secret("SLACK_USER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}

    # Sleep through work phase
    now = time.time()
    work_remaining = state["phase_end"] - now
    if work_remaining > 0:
        time.sleep(work_remaining)

    # Transition to break phase
    break_start = time.time()
    break_end = break_start + state["break_minutes"] * 60

    asyncio.run(_start_break_phase(headers, state["break_minutes"], break_end))

    # Update state file for break phase
    state["phase"] = "break"
    state["phase_start"] = break_start
    state["phase_end"] = break_end
    _write_state(state)

    # Sleep through break phase
    now = time.time()
    break_remaining = break_end - now
    if break_remaining > 0:
        time.sleep(break_remaining)

    # Session complete — clear Slack and remove state
    asyncio.run(_reset_slack(headers))
    _remove_state()


async def _start_break_phase(headers: dict, break_minutes: int, break_end: float) -> None:
    await _set_dnd(headers)  # end snooze
    await _update_status(headers, "On a short break", ":coffee:", int(break_end))


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="Pomodoro timer with Slack integration.")
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start a new pomodoro session")
    start_parser.add_argument("--work", type=int, default=25, help="Work phase in minutes (default: 25)")
    start_parser.add_argument("--break", type=int, default=5, dest="brk", help="Break phase in minutes (default: 5)")
    start_parser.add_argument("--status", type=str, default=None, help="Custom work status text (default: 'In Pomodoro session')")

    subparsers.add_parser("status", help="Check current pomodoro status")
    subparsers.add_parser("cancel", help="Cancel active pomodoro session")

    # Hidden daemon subcommand
    subparsers.add_parser("_daemon")

    args = parser.parse_args()

    match args.command:
        case "start":
            cmd_start(args)
        case "status":
            cmd_status(args)
        case "cancel":
            cmd_cancel(args)
        case "_daemon":
            cmd_daemon()
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
