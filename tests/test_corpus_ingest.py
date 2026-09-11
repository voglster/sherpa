"""Message ingest: cursors, tier order, thread sweep, idempotency, deletions.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.ingest import THREAD_SWEEP_WINDOW_SECONDS, ingest_workspace, message_id
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
