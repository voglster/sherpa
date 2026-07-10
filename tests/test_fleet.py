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
