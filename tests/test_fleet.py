"""Live-redis-guarded tests for the fleet tool.

Run: uv run --with pytest --with redis pytest tests/test_fleet.py
Skips automatically if no Redis is reachable at FLEET_REDIS_URL (default localhost:6379).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
import redis

_SPEC = importlib.util.spec_from_file_location(
    "fleet", Path(__file__).resolve().parent.parent / "tools" / "fleet.py"
)
fleet = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fleet)

REDIS_URL = os.environ.get("FLEET_REDIS_URL", "redis://localhost:6379")


@pytest.fixture
def run_ctx():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no Redis reachable")
    run = "pytest-" + os.urandom(3).hex()
    yield client, run
    for key in client.scan_iter(f"fleet:{run}:*"):
        client.delete(key)


def _env(monkeypatch, run, issue=None):
    monkeypatch.setenv("FLEET_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("FLEET_RUN", run)
    if issue is not None:
        monkeypatch.setenv("FLEET_ISSUE", str(issue))


def test_default_kickoff_requires_auto_close_keyword_in_body():
    prompt = fleet._kickoff_prompt("2", "run-x", "main", prompt_file=None)
    assert "Closes #2" in prompt
    assert "Fixes #2" in prompt
    assert "does NOT auto-close" in prompt


def test_prompt_file_override_bypasses_default_kickoff(tmp_path):
    override = tmp_path / "kickoff.txt"
    override.write_text("do the thing")
    assert fleet._kickoff_prompt("2", "run-x", "main", prompt_file=str(override)) == "do the thing"


def test_register_and_status_are_atomic_field_writes(run_ctx):
    client, run = run_ctx
    fleet._register_worker(client, run, "1")
    fleet._write_status(client, run, "1", {"state": "working", "commit": "abc123"})

    assert "1" in client.smembers(fleet.k_workers(run))
    status = client.hgetall(fleet.k_status(run, "1"))
    assert status["state"] == "working"
    assert status["commit"] == "abc123"
    assert "updated_at" in status

    # A second field-level write must not clobber the untouched commit field.
    fleet._write_status(client, run, "1", {"state": "ready"})
    status = client.hgetall(fleet.k_status(run, "1"))
    assert status["state"] == "ready"
    assert status["commit"] == "abc123"


def test_emit_event_appends_to_replayable_stream(run_ctx):
    client, run = run_ctx
    fleet._emit_event(client, run, "1", "report", {"state": "booting"})
    fleet._emit_event(client, run, "2", "report", {"state": "ready", "commit": "deadbeef"})

    events = fleet._flatten(client.xread({fleet.k_events(run): "0-0"}))
    assert [e["issue"] for e in events] == ["1", "2"]
    assert events[-1]["state"] == "ready"
    assert events[-1]["event"] == "report"


def test_cmd_report_then_status_roundtrip(run_ctx, monkeypatch, capsys):
    client, run = run_ctx
    _env(monkeypatch, run, issue="7")
    rc = fleet.cmd_report(
        argparse.Namespace(state="ready", commit="cafebabe", msg="done", run=None, issue=None)
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"

    rc = fleet.cmd_status(argparse.Namespace(issue="7", run=run, json=True))
    assert rc == 0
    worker = json.loads(capsys.readouterr().out)["workers"][0]
    assert worker["issue"] == "7"
    assert worker["state"] == "ready"
    assert worker["commit"] == "cafebabe"


def test_cmd_report_rejects_bad_state(run_ctx, monkeypatch):
    _, run = run_ctx
    _env(monkeypatch, run, issue="7")
    rc = fleet.cmd_report(
        argparse.Namespace(state="banana", commit=None, msg=None, run=None, issue=None)
    )
    assert rc == 1


def test_watch_returns_emitted_event_from_start(run_ctx, monkeypatch, capsys):
    client, run = run_ctx
    fleet._emit_event(client, run, "9", "report", {"state": "blocked", "msg": "need creds"})
    rc = fleet.cmd_watch(
        argparse.Namespace(run=run, timeout=2, from_id="0", count=100, json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["timed_out"] is False
    assert payload["events"][-1]["state"] == "blocked"


def test_ask_send_roundtrip_blocks_until_answered(run_ctx, monkeypatch):
    client, run = run_ctx
    _env(monkeypatch, run, issue="8")

    box: dict = {}

    def ask():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            box["rc"] = fleet.cmd_ask(
                argparse.Namespace(question="which file?", timeout=10, run=None, issue=None)
            )
        box["out"] = buf.getvalue()

    thread = threading.Thread(target=ask)
    thread.start()

    status_key = fleet.k_status(run, "8")
    for _ in range(50):
        if client.hget(status_key, "state") == "needs-decision":
            break
        time.sleep(0.1)
    assert client.hget(status_key, "state") == "needs-decision"
    assert thread.is_alive()  # genuinely blocked, not returned early

    with contextlib.redirect_stdout(io.StringIO()):
        fleet.cmd_send(
            argparse.Namespace(issue="8", message="edit config.yaml", interrupt=False, run=run)
        )

    thread.join(timeout=10)
    assert not thread.is_alive()
    answer = json.loads(box["out"])
    assert answer["answered"] is True
    assert answer["answer"] == "edit config.yaml"
    assert client.hget(status_key, "state") == "working"


def _with_socket_timeout(url: str, seconds: float) -> str:
    """Return `url` carrying a `socket_timeout` query param (mimics a shared/managed redis)."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["socket_timeout"] = str(seconds)
    return urlunparse(parts._replace(query=urlencode(query)))


def test_ask_survives_socket_timeout_shorter_than_block_window(run_ctx, monkeypatch):
    """Regression: a short `?socket_timeout=` must not sink `ask`'s long block.

    The overseer answers only AFTER the socket read timeout would have fired, so a raw
    `xread(block=...)` longer than socket_timeout raises "Timeout reading from socket".
    Routing through `_blocking_xread` re-issues the read across that timeout and still
    delivers the answer.
    """
    _client, run = run_ctx
    short_url = _with_socket_timeout(REDIS_URL, 1)
    _env(monkeypatch, run, issue="8")
    monkeypatch.setenv("FLEET_REDIS_URL", short_url)

    box: dict = {}

    def ask():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            box["rc"] = fleet.cmd_ask(
                argparse.Namespace(question="which file?", timeout=6, run=None, issue=None)
            )
        box["out"] = buf.getvalue()

    thread = threading.Thread(target=ask)
    thread.start()

    status_key = fleet.k_status(run, "8")
    for _ in range(50):
        if _client.hget(status_key, "state") == "needs-decision":
            break
        time.sleep(0.1)
    assert _client.hget(status_key, "state") == "needs-decision"

    # Answer only after the 1s socket_timeout has elapsed at least once, proving the
    # read is re-issued across the socket timeout rather than dying on it.
    time.sleep(2)
    assert thread.is_alive()  # not raised, not returned early
    with contextlib.redirect_stdout(io.StringIO()):
        fleet.cmd_send(
            argparse.Namespace(issue="8", message="edit config.yaml", interrupt=False, run=run)
        )

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert box["rc"] == 0
    answer = json.loads(box["out"])
    assert answer["answered"] is True
    assert answer["answer"] == "edit config.yaml"
    assert _client.hget(status_key, "state") == "working"


def test_inbox_wait_survives_socket_timeout_and_returns_empty(run_ctx, monkeypatch, capsys):
    """Regression: `inbox --wait` with a block window longer than a short socket_timeout
    must time out empty instead of raising a socket read timeout."""
    _client, run = run_ctx
    short_url = _with_socket_timeout(REDIS_URL, 1)
    _env(monkeypatch, run, issue="11")
    monkeypatch.setenv("FLEET_REDIS_URL", short_url)

    rc = fleet.cmd_inbox(
        argparse.Namespace(wait=True, timeout=2, count=100, run=run, issue=None, json=True)
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []


def test_inbox_cursor_advances(run_ctx, monkeypatch, capsys):
    client, run = run_ctx
    _env(monkeypatch, run, issue="11")
    with contextlib.redirect_stdout(io.StringIO()):
        fleet.cmd_send(argparse.Namespace(issue="11", message="rebase please", interrupt=False, run=run))

    rc = fleet.cmd_inbox(
        argparse.Namespace(wait=False, timeout=None, count=100, run=run, issue=None, json=True)
    )
    assert rc == 0
    first = json.loads(capsys.readouterr().out)["messages"]
    assert len(first) == 1
    assert first[0]["msg"] == "rebase please"

    rc = fleet.cmd_inbox(
        argparse.Namespace(wait=False, timeout=None, count=100, run=run, issue=None, json=True)
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []


class _FakeTmux:
    """Records tmux calls and replays a scripted sequence of pane captures."""

    def __init__(self, captures):
        self._captures = list(captures)
        self.calls = []
        self.pastes = 0
        self.enters = 0

    def tmux(self, *args, check=True):
        self.calls.append(args)
        if args[0] == "paste-buffer":
            self.pastes += 1
        if args[0] == "send-keys" and args[-1] == "Enter":
            self.enters += 1
        return argparse.Namespace(stdout="", returncode=0)

    def capture(self, target):
        return self._captures.pop(0) if len(self._captures) > 1 else self._captures[0]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)


def _wire(monkeypatch, fake):
    monkeypatch.setattr(fleet, "_tmux", fake.tmux)
    monkeypatch.setattr(fleet, "_tmux_capture", fake.capture)


def test_pane_ready_and_active_markers():
    assert fleet._pane_ready("...\n? for shortcuts")
    assert not fleet._pane_ready("Do you trust the files in this folder?")
    assert fleet._pane_active("✻ Thinking… (esc to interrupt)")
    assert not fleet._pane_active("? for shortcuts")


def test_paste_kickoff_waits_for_ready_then_confirms(monkeypatch, no_sleep):
    fake = _FakeTmux(["booting plugins…", "? for shortcuts", "esc to interrupt"])
    _wire(monkeypatch, fake)
    result = fleet._paste_kickoff("w", "hello", boot_wait=0, poll_interval=0)
    assert result == "confirmed"
    assert fake.pastes == 1


def test_paste_kickoff_retries_once_when_first_paste_swallowed(monkeypatch, no_sleep):
    # Ready immediately, but the pane never shows activity after the first paste;
    # after the retry it does.
    captures = ["? for shortcuts", "? for shortcuts", "esc to interrupt"]
    fake = _FakeTmux(captures)
    _wire(monkeypatch, fake)
    result = fleet._paste_kickoff(
        "w", "hello", boot_wait=0, activity_timeout=0, poll_interval=0
    )
    assert result == "confirmed"
    assert fake.pastes == 2


def test_paste_kickoff_reports_unconfirmed_after_two_failed_pastes(monkeypatch, no_sleep):
    fake = _FakeTmux(["? for shortcuts"])  # ready, but never active
    _wire(monkeypatch, fake)
    result = fleet._paste_kickoff(
        "w", "hello", boot_wait=0, activity_timeout=0, poll_interval=0
    )
    assert result == "unconfirmed"
    assert fake.pastes == 2


def test_paste_kickoff_reports_never_ready_when_prompt_absent(monkeypatch, no_sleep):
    fake = _FakeTmux(["Do you trust the files in this folder?"])
    _wire(monkeypatch, fake)
    result = fleet._paste_kickoff(
        "w", "hello", boot_wait=0, ready_timeout=0, activity_timeout=0, poll_interval=0
    )
    assert result == "never-ready"
    assert fake.pastes == 2  # still pastes as a best effort
def test_interrupt_send_resets_state_to_working(run_ctx, monkeypatch):
    """`send --interrupt` re-tasks the worker, so its stale reported state
    (e.g. ready) flips to working — the overseer shouldn't have to re-inspect
    the window. A plain queued send does NOT touch state."""
    client, run = run_ctx
    _env(monkeypatch, run, issue="42")
    status_key = fleet.k_status(run, "42")

    # Worker had reported ready.
    fleet._write_status(client, run, "42", {"state": "ready", "commit": "abc123"})

    # Plain send (no interrupt): state must NOT change (worker hasn't seen it).
    with contextlib.redirect_stdout(io.StringIO()):
        fleet.cmd_send(
            argparse.Namespace(issue="42", message="fyi", interrupt=False, run=run)
        )
    assert client.hget(status_key, "state") == "ready"

    # Interrupt send: state flips to working (tmux nudge stubbed out).
    monkeypatch.setattr(fleet, "_interrupt_worker", lambda *a, **k: True)
    with contextlib.redirect_stdout(io.StringIO()):
        fleet.cmd_send(
            argparse.Namespace(issue="42", message="rework the selector", interrupt=True, run=run)
        )
    assert client.hget(status_key, "state") == "working"
