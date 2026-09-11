"""Message ingest: cursors, tier order, thread sweep, idempotency, deletions.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_ingest.py -v
"""

from __future__ import annotations

import json
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
    "auth.test": [{"ok": True, "user_id": "U07JIMVOG"}],
    "users.list": [{"ok": True, "members": [
        {"id": "U07JIMVOG", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        {"id": "U03K2ABCD", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
    ]}],
    "conversations.list": [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U03K2ABCD"},
    ]}],
}


def client_with(history, replies=None):
    pages = dict(DIRECTORY)
    pages["conversations.history"] = history
    pages["conversations.replies"] = replies or [{"ok": True, "messages": []}]
    return FakeSlack(pages)


class SlackHistory:
    """`conversations.history` as Slack actually serves it.

    Messages come back NEWEST-FIRST, cut to `limit`, bounded by the exclusive
    `oldest`/`latest` window, with `has_more` and a `next_cursor` that resumes
    below the page just returned. A fixture that hands back ascending messages
    instead is the one ordering under which a broken cursor looks correct, so
    every history fixture here goes through this server.
    """

    def __init__(self, messages: list[dict]) -> None:
        self.messages = sorted(messages, key=lambda m: float(m["ts"]), reverse=True)

    def add(self, *messages: dict) -> None:
        self.messages = sorted(
            [*self.messages, *messages], key=lambda m: float(m["ts"]), reverse=True
        )

    def __call__(self, **params) -> dict:
        oldest = float(params["oldest"]) if params.get("oldest") else None
        latest = float(params["latest"]) if params.get("latest") else None
        if params.get("cursor"):
            cursor = float(params["cursor"])
            latest = cursor if latest is None else min(latest, cursor)
        limit = params.get("limit") or 200

        window = [
            message
            for message in self.messages
            if (oldest is None or float(message["ts"]) > oldest)
            and (latest is None or float(message["ts"]) < latest)
        ]
        page, rest = window[:limit], window[limit:]
        body = {"ok": True, "messages": page, "has_more": bool(rest)}
        if rest:
            body["response_metadata"] = {"next_cursor": page[-1]["ts"]}
        return body


def client_serving(histories: dict[str, SlackHistory], *, replies=None, channels=None):
    pages = dict(DIRECTORY)
    if channels is not None:
        pages["conversations.list"] = [{"ok": True, "channels": channels}]
    pages["conversations.replies"] = replies or [{"ok": True, "messages": []}]
    client = FakeSlack(pages)
    directory_call = client.call

    def call(method, **params):
        if method != "conversations.history":
            return directory_call(method, **params)
        client.calls.append((method, params))
        return histories[params["channel"]](**params)

    client.call = call
    return client



def serve_threads(client, threads: dict[str, list[dict]]):
    """`conversations.replies` as Slack serves it: the thread you asked for.

    A fixture that replays one canned thread for every `ts` hides both halves of
    the timestamp bug -- the request never has to be right, and the reply set
    never has to belong to the parent.
    """
    original = client.call

    def call(method, **params):
        if method != "conversations.replies":
            return original(method, **params)
        client.calls.append((method, params))
        if params["ts"] not in threads:
            raise SlackError("thread_not_found")
        return {"ok": True, "messages": threads[params["ts"]]}

    client.call = call
    return client


def _backfill_complete(conn, channel_id: str = "D1") -> bool:
    row = conn.execute(
        "SELECT complete FROM backfill_state WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return bool(row and row[0])


def history_params(client, *, only_with=None) -> list[dict]:
    return [
        params
        for method, params in client.calls
        if method == "conversations.history" and (only_with is None or params.get(only_with))
    ]


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
        {"ts": "100.0", "user": "U07JIMVOG", "text": "I'll have the schema thursday"},
        {"ts": "99.0", "user": "U03K2ABCD", "text": "can you take the cli work?"},
    ]}])
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert report.messages == 2
    rows = dict(conn.execute("SELECT author_name, is_me FROM messages"))
    assert rows == {"Jim Vogel": 1, "Hunter Reeves": 0}


def test_ingest_advances_the_cursor_to_the_newest_ts(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "100.0", "user": "U03K2ABCD", "text": "a"},
        {"ts": "99.0", "user": "U03K2ABCD", "text": "b"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0] == 100.0


def test_second_run_requests_only_messages_after_the_cursor(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U03K2ABCD", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    history_calls = [params for method, params in client.calls if method == "conversations.history"]
    assert history_calls and history_calls[0]["oldest"] == "100.0"


def test_reingesting_the_same_message_does_not_duplicate_it(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U03K2ABCD", "text": "a"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_edited_message_text_is_updated_in_place(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [{"ts": "100.0", "user": "U03K2ABCD", "text": "before"}]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    conn.execute("UPDATE sync_state SET last_ts = 0")
    client._pages["conversations.history"] = [
        {"ok": True, "messages": [{"ts": "100.0", "user": "U03K2ABCD", "text": "after", "edited": {"ts": "101.0"}}]}
    ]
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT text FROM messages").fetchone()[0] == "after"
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'after'"
    ).fetchone()[0] == 1


def test_tombstone_messages_are_not_stored(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "101.0", "user": "U03K2ABCD", "text": "This message was deleted.", "subtype": "tombstone"},
        {"ts": "100.0", "user": "U03K2ABCD", "text": "real"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_thread_sweep_pulls_replies_for_parents_with_reply_count(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
            {"ts": "100.5", "user": "U07JIMVOG", "text": "on it", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None)
    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"parent", "on it"}


def test_thread_sweep_finds_new_replies_on_old_threads_across_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
        {"ts": "100.5", "user": "U07JIMVOG", "text": "late reply", "thread_ts": "100.0"},
    ]}]
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "late reply" in texts


def test_thread_sweep_skips_parents_older_than_the_recency_window(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 100.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
        {"ts": "100.5", "user": "U07JIMVOG", "text": "late reply", "thread_ts": "100.0"},
    ]}]
    stale_now = 100.0 + THREAD_SWEEP_WINDOW_SECONDS + 1000.0
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: stale_now)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "late reply" not in texts


def test_thread_sweep_does_not_duplicate_already_stored_replies(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
            {"ts": "100.5", "user": "U07JIMVOG", "text": "on it", "thread_ts": "100.0"},
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
            {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0"},
            {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS - 100.0}", "user": "U07JIMVOG",
             "text": "still going", "thread_ts": "0.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 0.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS - 100.0}", "user": "U07JIMVOG",
         "text": "still going", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS + 40.0}", "user": "U03K2ABCD",
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
            {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0", "reply_count": 1},
        ]}],
        replies=[{"ok": True, "messages": [
            {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0"},
            {"ts": "50.0", "user": "U07JIMVOG", "text": "last word", "thread_ts": "0.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 0.0)

    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    client._pages["conversations.replies"] = [{"ok": True, "messages": [
        {"ts": "0.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "0.0"},
        {"ts": "50.0", "user": "U07JIMVOG", "text": "last word", "thread_ts": "0.0"},
        {"ts": f"{THREAD_SWEEP_WINDOW_SECONDS + 5000.0}", "user": "U03K2ABCD",
         "text": "should never arrive", "thread_ts": "0.0"},
    ]}]
    long_after_now = THREAD_SWEEP_WINDOW_SECONDS + 10000.0
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: long_after_now)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "should never arrive" not in texts


def test_an_interrupted_pass_over_newest_first_history_loses_nothing(tmp_path):
    """The defect this module exists to avoid: history arrives newest-first, so
    a run cut off by the budget has stored only the top of the range. Anything
    that pins a cursor there buries the rest of that page below it forever."""
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([{"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"m{i}"} for i in range(1, 1001)])
    client = client_serving({"D1": history})
    clock = TickingClock()

    first = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=6.0, now=clock)
    assert 0 < first.messages < 1000  # the run really was cut short

    for _ in range(30):
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=6.0, now=clock)

    stored = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert stored == {f"m{i}" for i in range(1, 1001)}


def test_a_first_backfill_terminates_instead_of_refetching_the_newest_pages(tmp_path):
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([{"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"m{i}"} for i in range(1, 1001)])
    client = client_serving({"D1": history})
    clock = TickingClock()

    runs = 0
    while not _backfill_complete(conn) and runs < 30:
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=6.0, now=clock)
        runs += 1

    assert _backfill_complete(conn)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1000

    backward = [params["latest"] for params in history_params(client, only_with="latest")]
    assert len(backward) == len(set(backward))  # each page picks up below the last, never restarts


def test_a_completed_backfill_stops_asking_for_older_pages(tmp_path):
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([{"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"m{i}"} for i in range(1, 301)])
    client = client_serving({"D1": history})
    clock = TickingClock()

    for _ in range(10):
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0, now=clock)
    assert _backfill_complete(conn)

    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0, now=clock)
    assert history_params(client, only_with="latest") == []


def test_a_channel_empty_at_first_ingest_still_ingests_later_messages(tmp_path):
    """A brand-new DM is empty the moment it first appears in conversations.list,
    so this is the first conversation with a new colleague, not a curiosity."""
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([])
    client = client_serving({"D1": history})

    ingest_workspace(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    history.add({"ts": "100.0", "user": "U03K2ABCD", "text": "hey, welcome aboard"})
    ingest_workspace(conn, client, "gravitate", me=None)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"hey, welcome aboard"}
    assert conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0] == 100.0


def test_a_channel_that_stays_empty_is_not_paged_backwards_again(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_serving({"D1": SlackHistory([])})

    ingest_workspace(conn, client, "gravitate", me=None)
    client.calls.clear()
    report = ingest_workspace(conn, client, "gravitate", me=None)

    assert report.messages == 0
    assert report.channels_failed == []
    assert history_params(client, only_with="latest") == []
    # one forward look and nothing else: a finished backfill is not re-attempted
    # just because the channel never had anything below the cursor.
    assert len(history_params(client)) == 1


def test_a_completed_backfill_still_catches_up_on_new_messages(tmp_path):
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([{"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"m{i}"} for i in range(1, 301)])
    client = client_serving({"D1": history})

    ingest_workspace(conn, client, "gravitate", me=None)
    assert _backfill_complete(conn)

    history.add({"ts": "900.0", "user": "U03K2ABCD", "text": "posted after the backfill finished"})
    ingest_workspace(conn, client, "gravitate", me=None)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "posted after the backfill finished" in texts
    assert len(texts) == 301


def test_a_swept_reply_does_not_advance_the_history_cursor(tmp_path):
    """A reply can be newer than main-channel messages the run has not reached
    yet; letting it raise the history cursor steps straight over them."""
    conn = open_db(tmp_path / "c.db")
    history = SlackHistory([
        {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0", "reply_count": 1},
    ])
    client = client_serving(
        {"D1": history},
        replies=[{"ok": True, "messages": [
            {"ts": "100.0", "user": "U03K2ABCD", "text": "parent", "thread_ts": "100.0"},
            {"ts": "500.0", "user": "U07JIMVOG", "text": "reply from the future", "thread_ts": "100.0"},
        ]}],
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 500.0)

    assert conn.execute(
        "SELECT last_ts FROM sync_state WHERE channel_id='D1'"
    ).fetchone()[0] == 100.0

    history.add(
        {"ts": "200.0", "user": "U03K2ABCD", "text": "posted while the thread ran"},
        {"ts": "300.0", "user": "U03K2ABCD", "text": "and another"},
    )
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: 500.0)

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert {"posted while the thread ran", "and another"} <= texts


def test_sweep_caps_parents_per_run_and_covers_the_backlog_over_successive_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    thread_count = THREAD_SWEEP_PER_CHANNEL_CAP + 5
    parents = [
        {"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"parent{i}", "thread_ts": f"{i}.0", "reply_count": 1}
        for i in range(thread_count, 0, -1)
    ]
    client = client_with(history=[{"ok": True, "messages": parents}])
    serve_threads(client, {
        parent["ts"]: [
            {"ts": parent["ts"], "user": "U03K2ABCD", "text": parent["text"], "thread_ts": parent["ts"]},
            {"ts": f"{float(parent['ts']) + 0.5}", "user": "U07JIMVOG", "text": f"reply to {parent['text']}",
             "thread_ts": parent["ts"]},
        ]
        for parent in parents
    })
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
    huge_backlog = [{"ts": f"{i}.0", "user": "U03K2ABCD", "text": f"m{i}"} for i in range(1, 5000)]
    small_backlog = [{"ts": "5.0", "user": "U03K2ABCD", "text": "hi from C3"}]
    client = client_serving(
        {"D1": SlackHistory(huge_backlog), "C3": SlackHistory(small_backlog)},
        channels=[
            {"id": "D1", "is_im": True, "user": "U03K2ABCD"},
            {"id": "C3", "name": "other", "is_channel": True, "is_member": True},
        ],
    )

    clock = TickingClock()
    report = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=20.0, now=clock)

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
        {"id": "D1", "is_im": True, "user": "U03K2ABCD"},
        {"id": "C2", "name": "other", "is_channel": True, "is_member": True},
    ]}]
    calls = {"n": 0}
    client = FakeSlack(pages)
    original = client.call

    def flaky(method, **params):
        if method == "conversations.history" and params.get("channel") == "D1":
            raise SlackError("channel_not_found")
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "5.0", "user": "U03K2ABCD", "text": "hi"}]}
        return original(method, **params)

    client.call = flaky
    report = ingest_workspace(conn, client, "gravitate", me=None)
    assert [c for c, _ in report.channels_failed] == ["D1"]
    assert report.messages == 1


def test_message_id_is_stable_and_namespaced():
    assert message_id("gravitate", "D1", "100.0") == "gravitate:D1:100.0"


# --- Slack timestamps are strings, not floats -------------------------------
#
# A Slack ts has exactly six fractional digits and is matched by the API as the
# literal string. `str(float("1789151200.000100"))` is "1789151200.0001", which
# matches no message: conversations.replies answers `thread_not_found`. Every
# fixture below uses a realistic six-digit ts, because "100.0" is the only shape
# under which a float round-trip looks correct.

PARENT_TS = "1757606400.000100"
REPLY_TS = "1757606460.000200"


def _threaded_client(*, replies=None):
    return client_with(
        history=[{"ok": True, "messages": [
            {"ts": PARENT_TS, "user": "U03K2ABCD", "text": "parent",
             "thread_ts": PARENT_TS, "reply_count": 1},
        ]}],
        replies=replies or [{"ok": True, "messages": [
            {"ts": PARENT_TS, "user": "U03K2ABCD", "text": "parent", "thread_ts": PARENT_TS},
            {"ts": REPLY_TS, "user": "U07JIMVOG", "text": "on it", "thread_ts": PARENT_TS},
        ]}],
    )


def test_sweep_asks_slack_for_the_exact_thread_ts_string(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _threaded_client()
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    asked = [params["ts"] for method, params in client.calls if method == "conversations.replies"]
    assert asked == [PARENT_TS]


def test_a_thread_parent_already_swept_this_run_is_not_swept_twice(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _threaded_client()
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    asked = [params["ts"] for method, params in client.calls if method == "conversations.replies"]
    assert len(asked) == len(set(asked))


def test_one_broken_thread_does_not_fail_the_whole_channel(tmp_path):
    conn = open_db(tmp_path / "c.db")
    other_parent = "1757606500.000300"
    client = client_with(
        history=[{"ok": True, "messages": [
            {"ts": PARENT_TS, "user": "U03K2ABCD", "text": "parent",
             "thread_ts": PARENT_TS, "reply_count": 1},
            {"ts": other_parent, "user": "U03K2ABCD", "text": "other parent",
             "thread_ts": other_parent, "reply_count": 1},
        ]}],
    )
    original = client.call

    def flaky(method, **params):
        if method == "conversations.replies" and params.get("ts") == PARENT_TS:
            raise SlackError("ratelimited")
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                {"ts": other_parent, "user": "U03K2ABCD", "text": "other parent",
                 "thread_ts": other_parent},
                {"ts": "1757606560.000400", "user": "U07JIMVOG", "text": "survived",
                 "thread_ts": other_parent},
            ]}
        return original(method, **params)

    client.call = flaky
    report = ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "survived" in texts
    assert report.channels_failed == []
    assert [t for _, t, _ in report.threads_failed] == [PARENT_TS]
    assert conn.execute(
        "SELECT status FROM sync_state WHERE channel_id = 'D1'"
    ).fetchone()[0] == "ok"


# --- Thread replies during backfill -----------------------------------------
#
# A channel with backlog exhausts the history budget on every run, so anything
# the sweep is asked to pay for out of the leftovers is paid nothing for the
# whole length of the backfill -- days on a real workspace. A parent discovered
# there and held only in a local set is dropped, and its replies are then
# unrecoverable: a backfilled thread with no stored replies has MAX(ts) equal to
# the parent's own ts, which is outside the sweep's recency window forever.

OLD_PARENT_TS = "1700049000.000100"
OLD_REPLY_TS = "1700049100.000200"


def test_a_thread_found_during_a_budget_exhausted_backfill_is_still_swept(tmp_path):
    conn = open_db(tmp_path / "c.db")
    backlog = [
        {"ts": f"{1700000000 + i}.000500", "user": "U03K2ABCD", "text": f"m{i}"}
        for i in range(1, 40000)
    ]
    parent = {"ts": OLD_PARENT_TS, "user": "U03K2ABCD", "text": "old parent",
              "thread_ts": OLD_PARENT_TS, "reply_count": 1}
    history = SlackHistory([*backlog, parent])
    client = client_serving({"D1": history})
    serve_threads(client, {OLD_PARENT_TS: [
        parent,
        {"ts": OLD_REPLY_TS, "user": "U07JIMVOG", "text": "the promise in the thread",
         "thread_ts": OLD_PARENT_TS},
    ]})

    clock = TickingClock(start=1800000000.0)
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=100.0, now=clock)

    assert not _backfill_complete(conn)  # the backfill really is still running
    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert "the promise in the thread" in texts


def test_a_thread_parent_is_recorded_the_moment_it_is_seen(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _threaded_client()
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    assert conn.execute(
        "SELECT thread_ts_raw FROM thread_sweeps WHERE channel_id = 'D1'"
    ).fetchone()[0] == PARENT_TS


# --- Mentions, channel refs and links ---------------------------------------
#
# Slack stores a mention as `<@U03K2ABCD>`, never as a name. In a channel,
# mentioning someone is the primary way their name appears at all, so leaving
# the raw form in the text means neither the FTS index nor the embedding can
# find it -- exactly the "what did I promise Hunter" case.

def test_a_mention_is_stored_as_the_persons_name(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U07JIMVOG",
         "text": "<@U03K2ABCD> I'll get you the schema"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)

    assert conn.execute("SELECT text FROM messages").fetchone()[0] == (
        "@Hunter Reeves I'll get you the schema"
    )
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'Hunter'"
    ).fetchone()[0] == 1


def test_channel_refs_links_and_escapes_are_rendered_readable(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U03K2ABCD",
         "text": "see <#C123|gravi-cli> and <https://example.com/x|the writeup>"
                 " &amp; <!here> &lt;not a tag&gt;"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)

    stored = conn.execute("SELECT text FROM messages").fetchone()[0]
    assert "#gravi-cli" in stored
    assert "the writeup (https://example.com/x)" in stored
    assert "@here" in stored
    assert "&" in stored and "&amp;" not in stored
    assert "<not a tag>" in stored


def test_the_raw_slack_text_stays_recoverable(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U07JIMVOG", "text": "<@U03K2ABCD> ping"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)

    meta = json.loads(conn.execute("SELECT meta FROM messages").fetchone()[0])
    assert meta["raw_text"] == "<@U03K2ABCD> ping"
    assert meta["mentions"] == ["Hunter Reeves"]


def test_an_unresolvable_mention_keeps_its_id_rather_than_vanishing(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U03K2ABCD", "text": "ask <@U09NOBODY>"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me=None)

    assert "U09NOBODY" in conn.execute("SELECT text FROM messages").fetchone()[0]


def test_a_tombstoned_message_is_removed_from_the_corpus(tmp_path):
    """Slack replaces a deleted message with a tombstone in the thread it was
    in. That is one of only two deletions the API lets this corpus observe --
    conversations.history simply omits deleted messages, so a plain channel
    message deleted below the cursor cannot be detected at all."""
    conn = open_db(tmp_path / "c.db")
    parent = {"ts": PARENT_TS, "user": "U03K2ABCD", "text": "parent",
              "thread_ts": PARENT_TS, "reply_count": 1}
    reply = {"ts": REPLY_TS, "user": "U07JIMVOG", "text": "regrettable",
             "thread_ts": PARENT_TS}
    client = client_with(history=[{"ok": True, "messages": [parent]}])
    serve_threads(client, {PARENT_TS: [parent, reply]})
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2

    serve_threads(client, {PARENT_TS: [
        parent,
        {"ts": REPLY_TS, "user": "U07JIMVOG", "text": "This message was deleted.",
         "subtype": "tombstone", "thread_ts": PARENT_TS},
    ]})
    report = ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"parent"}
    assert "D1" in report.channels_touched


def test_a_reply_slack_no_longer_serves_is_dropped(tmp_path):
    conn = open_db(tmp_path / "c.db")
    parent = {"ts": PARENT_TS, "user": "U03K2ABCD", "text": "parent",
              "thread_ts": PARENT_TS, "reply_count": 1}
    reply = {"ts": REPLY_TS, "user": "U07JIMVOG", "text": "vanishes",
             "thread_ts": PARENT_TS}
    client = client_with(history=[{"ok": True, "messages": [parent]}])
    serve_threads(client, {PARENT_TS: [parent, reply]})
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    serve_threads(client, {PARENT_TS: [parent]})
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    texts = {row[0] for row in conn.execute("SELECT text FROM messages")}
    assert texts == {"parent"}


def test_a_deleted_thread_parent_takes_its_replies_with_it(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _threaded_client()
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2

    serve_threads(client, {})  # the parent is gone: thread_not_found
    client._pages["conversations.history"] = [{"ok": True, "messages": []}]
    ingest_workspace(conn, client, "gravitate", me=None, now=lambda: float(REPLY_TS))

    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
