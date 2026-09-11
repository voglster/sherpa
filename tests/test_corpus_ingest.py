"""Message ingest: cursors, tier order, thread sweep, idempotency, deletions.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.ingest import (
    THREAD_SWEEP_PER_CHANNEL_CAP,
    THREAD_SWEEP_WINDOW_SECONDS,
    ingest_workspace,
    message_id,
)
from sherpa.corpus.slack import AuthError, FakeSlack, SlackError

DIRECTORY = {
    "auth.test": [{"ok": True, "user_id": "U_ME"}],
    "users.list": [{"ok": True, "members": [
        {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        {"id": "U_H", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
    ]}],
    "conversations.list": [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
    ]}],
}


def client_with(history, replies=None):
    pages = dict(DIRECTORY)
    pages["conversations.history"] = history
    pages["conversations.replies"] = replies or [{"ok": True, "messages": []}]
    return FakeSlack(pages)


class TickingClock:
    """A fake wall clock that advances by one second per call, so budget math
    in the ingester is exercised deterministically instead of racing the real
    clock. Shared across successive `ingest_workspace` calls in a test so time
    keeps moving forward across runs, the way it would in production."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def test_ingest_stores_messages_with_resolved_names_and_is_me(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_ME", "text": "I'll have the schema thursday"},
        {"ts": "99.0", "user": "U_H", "text": "can you take the cli work?"},
    ]}])
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert report.messages == 2
    rows = dict(conn.execute("SELECT author_name, is_me FROM messages"))
    assert rows == {"Jim Vogel": 1, "Hunter Reeves": 0}


def test_ingest_advances_the_cursor_to_the_newest_ts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "a"},
        {"ts": "99.0", "user": "U_H", "text": "b"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0] == 100.0


def test_second_run_requests_only_messages_after_the_cursor(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    history_calls = [params for method, params in client.calls if method == "conversations.history"]
    assert history_calls and history_calls[0]["oldest"] == "100.0"


def test_reingesting_the_same_message_does_not_duplicate_it(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_edited_message_text_is_updated_in_place(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "before"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    client._pages["conversations.history"] = [
        {"ok": True, "messages": [{"ts": "100.0", "user": "U_H", "text": "after", "edited": {"ts": "101.0"}}]}
    ]
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT text FROM messages").fetchone()[0] == "after"
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'after'"
    ).fetchone()[0] == 1


def test_tombstone_messages_are_not_stored(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "real"},
        {"ts": "101.0", "user": "U_H", "text": "This message was deleted.", "subtype": "tombstone"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_thread_sweep_pulls_replies_for_parents_with_reply_count(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
            {"ts": "100.5", "user": "U_ME", "text": "on it", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None)
    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"parent", "on it"}


def test_thread_sweep_finds_new_replies_on_old_threads_across_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
        {"ts": "100.5", "user": "U_ME", "text": "late reply", "thread_ts": "100.0"},
    ]}]
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "late reply" in texts


def test_thread_sweep_skips_parents_older_than_the_recency_window(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
        {"ts": "100.5", "user": "U_ME", "text": "late reply", "thread_ts": "100.0"},
    ]}]
    stale_now = 100.0 + THREAD_SWEEP_WINDOW_SECONDS + 1000.0
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: stale_now)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "late reply" not in texts


def test_thread_sweep_does_not_duplicate_already_stored_replies(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U_H", "text": "parent", "thread_ts": "100.0"},
            {"ts": "100.5", "user": "U_ME", "text": "on it", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_thread_sweep_uses_latest_reply_not_parent_age_for_the_window(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0"},
            {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS - 100.0}", "user": "U_ME",
             "text": "still going", "thread_ts": "0.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 0.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS - 100.0}", "user": "U_ME",
         "text": "still going", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS + 40.0}", "user": "U_H",
         "text": "one more thing", "thread_ts": "0.0"},
    ]}]
    later_now = THREAD_SWEEP_WINDOW_SECONDS + 50.0
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: later_now)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "one more thing" in texts


def test_thread_sweep_skips_threads_dead_for_the_entire_window(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0"},
            {"ts": "50.0", "user": "U_ME", "text": "last word", "thread_ts": "0.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 0.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "0.0", "user": "U_H", "text": "parent", "thread_ts": "0.0"},
        {"ts": "50.0", "user": "U_ME", "text": "last word", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS + 5000.0}", "user": "U_H",
         "text": "should never arrive", "thread_ts": "0.0"},
    ]}]
    long_after_now = THREAD_SWEEP_WINDOW_SECONDS + 10000.0
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: long_after_now)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "should never arrive" not in texts


def test_history_pagination_stops_at_the_budget_and_resumes_without_loss(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": f"{i}.0", "user": "U_H", "text": f"m{i}"} for i in range(1, 61)
    ]}])
    clock = TickingClock()
    report = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100.0, now=clock)

    stored = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert 0 < stored < 60
    assert report.messages == stored
    last_ts = conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0]
    assert last_ts == stored  # the newest message actually stored, not the newest fetched

    remaining = [
        {"ts": f"{i}.0", "user": "U_H", "text": f"m{i}"} for i in range(1, 61) if float(f"{i}.0") > last_ts
    ]
    client._pages["conversations.history"] = [{"ok": True, "messages": remaining}]
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100.0, now=clock)

    total_stored = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    distinct_texts = conn.execute("SELECT count(DISTINCT text) FROM messages").fetchone()[0]
    assert total_stored == 60
    assert distinct_texts == 60


def test_sweep_caps_parents_per_run_and_covers_the_backlog_over_successive_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    thread_count = THREAD_SWEEP_PER_CHANNEL_CAP + 5
    parents = [
        {"ts": f"{i}.0", "user": "U_H", "text": f"parent{i}", "thread_ts": f"{i}.0", "reply_count": 1}
        for i in range(1, thread_count + 1)
    ]
    client = client_with(
        history=[{"ok": True, "messages": parents}],
        replies=[{"ok": True, "messages": [
            {"ts": "1.0", "user": "U_H", "text": "reply-echo", "thread_ts": "1.0"},
        ]}],
    )
    clock = TickingClock()
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100000.0, now=clock)

    def swept_thread_ids() -> set[str]:
        return {params["ts"] for method, params in client.calls if method == "conversations.replies"}

    client.calls.clear()
    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100000.0, now=clock)
    run2_swept = swept_thread_ids()
    assert len(run2_swept) <= THREAD_SWEEP_PER_CHANNEL_CAP

    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100000.0, now=clock)
    run3_swept = swept_thread_ids()
    assert len(run3_swept) <= THREAD_SWEEP_PER_CHANNEL_CAP

    all_thread_ids = {f"{i}.0" for i in range(1, thread_count + 1)}
    assert run2_swept | run3_swept == all_thread_ids


def test_a_busy_tier_one_channel_does_not_starve_tier_three(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
        {"id": "C3", "name": "other", "is_channel": True, "is_member": True},
    ]}]

    huge_backlog = [{"ts": f"{i}.0", "user": "U_H", "text": f"m{i}"} for i in range(1, 2000)]
    small_backlog = [{"ts": "5.0", "user": "U_H", "text": "hi from C3"}]

    def history(method, **params):
        messages = huge_backlog if params.get("channel") == "D1" else small_backlog
        return {"ok": True, "messages": messages}

    pages["conversations.history"] = [{"ok": True, "messages": []}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    client = FakeSlack(pages)
    original_call = client.call
    client.call = lambda method, **params: (
        history(method, **params) if method == "conversations.history" else original_call(method, **params)
    )

    clock = TickingClock()
    report = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100.0, now=clock)

    d1_stored = conn.execute("SELECT count(*) FROM messages WHERE channel_id = 'D1'").fetchone()[0]
    c3_stored = conn.execute("SELECT count(*) FROM messages WHERE channel_id = 'C3'").fetchone()[0]
    assert d1_stored < len(huge_backlog)
    assert c3_stored > 0
    assert "C3" not in [c for c, _ in report.channels_failed]


def test_auth_error_propagates_rather_than_being_swallowed(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.history"] = [{"ok": False, "error": "token_revoked"}]
    with pytest.raises(AuthError):
        ingest_workspace(conn, FakeSlack(pages), "gravitate", me=None)


def test_one_failing_channel_is_recorded_and_others_continue(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
        {"id": "C2", "name": "other", "is_channel": True, "is_member": True},
    ]}]
    calls = {"n": 0}
    client = FakeSlack(pages)
    original = client.call

    def flaky(method, **params):
        if method == "conversations.history" and params.get("channel") == "D1":
            raise SlackError("channel_not_found")
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "5.0", "user": "U_H", "text": "hi"}]}
        return original(method, **params)

    client.call = flaky
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert [c for c, _ in report.channels_failed] == ["D1"]
    assert report.messages == 1


def test_message_id_is_stable_and_namespaced():
    assert message_id("gravitate", "D1", "100.0") == "gravitate:D1:100.0"
