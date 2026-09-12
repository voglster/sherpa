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
    UNAVAILABLE_RETRY_SECONDS,
    TIER_BUDGET_SHARE,
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


def test_one_transiently_failing_channel_is_recorded_and_others_continue(tmp_path):
    """A transient error, because a permanent one is now set aside rather than
    counted as an outstanding fault -- see the unavailable tests below."""
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
            raise SlackError("ratelimited")
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


# --- The team's real Slack host ---------------------------------------------
#
# Found against the user's actual workspace: the company renamed but the Slack
# subdomain did not, so a workspace configured as "gravitate" lives at
# capspire.slack.com. Building the permalink from the config name produces a
# dead link for every message in the corpus, and permalinks are how an agent's
# answer leads back to the real conversation. No fixture could catch this on
# its own, because a fixture invents both halves.

RENAMED_DIRECTORY = {
    "auth.test": [{"ok": True, "user_id": "U07JIMVOG", "team": "Gravitate",
                   "url": "https://capspire.slack.com/"}],
    "users.list": DIRECTORY["users.list"],
    "conversations.list": DIRECTORY["conversations.list"],
}


def _renamed_client(history):
    pages = dict(RENAMED_DIRECTORY)
    pages["conversations.history"] = history
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    return FakeSlack(pages)


def test_permalinks_use_the_teams_real_host_not_the_config_name(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _renamed_client([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U03K2ABCD", "text": "hi"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me="U07JIMVOG")

    permalink = conn.execute("SELECT permalink FROM messages").fetchone()[0]
    assert permalink == (
        "https://capspire.slack.com/archives/D1/p1757606400000100"
    )


def test_a_learned_host_repairs_permalinks_already_stored(tmp_path):
    conn = open_db(tmp_path / "c.db")
    history = [{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U03K2ABCD", "text": "hi"},
    ]}]
    ingest_workspace(conn, client_with(history), "gravitate", me="U07JIMVOG")
    assert "gravitate.slack.com" in conn.execute(
        "SELECT permalink FROM messages"
    ).fetchone()[0]

    ingest_workspace(conn, _renamed_client(history), "gravitate", me="U07JIMVOG")
    assert "capspire.slack.com" in conn.execute(
        "SELECT permalink FROM messages"
    ).fetchone()[0]


def test_a_workspace_with_no_known_host_still_gets_a_permalink(tmp_path):
    """An older row, or a run before the host was learned, falls back to the
    previous behavior rather than emitting something malformed."""
    conn = open_db(tmp_path / "c.db")
    client = client_with([{"ok": True, "messages": [
        {"ts": "1757606400.000100", "user": "U03K2ABCD", "text": "hi"},
    ]}])
    ingest_workspace(conn, client, "gravitate", me="U07JIMVOG")

    assert conn.execute("SELECT permalink FROM messages").fetchone()[0] == (
        "https://gravitate.slack.com/archives/D1/p1757606400000100"
    )


def test_the_directory_walk_answers_to_the_runs_budget(tmp_path):
    """1,382 group DMs at one membership call each, before the deadline was
    even computed, made a --budget 120 run take about seven minutes with no
    messages fetched. The walk is part of the run and answers to its clock."""
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": f"G{i}", "is_mpim": True, "name": f"mpdm-jim--hunter-{i}"} for i in range(60)
    ]}]
    pages["conversations.members"] = [{"ok": True, "members": ["U07JIMVOG", "U03K2ABCD"]}]
    pages["conversations.history"] = [{"ok": True, "messages": []}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    client = FakeSlack(pages)

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=40.0, now=TickingClock())

    member_calls = [1 for method, _ in client.calls if method == "conversations.members"]
    assert 0 < len(member_calls) < 60


def test_a_bot_author_is_named_rather_than_left_as_a_raw_id(tmp_path):
    """Bots are absent from users.list, so resolution fell through to the raw
    id and `B01T0JT986T` appeared as an author name. Excluded from chunks or
    not, that name shows up in `messages` and in with/owed transcripts."""
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.history"] = [{"ok": True, "messages": [
        {"ts": "1757606400.000100", "bot_id": "B01T0JT986T",
         "text": "Cullen Denison created an Issue GSD-9015"},
    ]}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    pages["bots.info"] = [{"ok": True, "bot": {"id": "B01T0JT986T", "name": "Jira"}}]
    ingest_workspace(conn, FakeSlack(pages), "gravitate", me=None)

    assert conn.execute("SELECT author_name FROM messages").fetchone()[0] == "Jira"
    assert conn.execute(
        "SELECT is_bot FROM users WHERE id = 'B01T0JT986T'"
    ).fetchone()[0] == 1


def test_an_unresolvable_bot_is_asked_about_only_once(tmp_path):
    conn = open_db(tmp_path / "c.db")
    pages = dict(DIRECTORY)
    pages["conversations.history"] = [{"ok": True, "messages": [
        {"ts": "1757606400.000100", "bot_id": "B0GONE", "text": "one"},
        {"ts": "1757606401.000100", "bot_id": "B0GONE", "text": "two"},
    ]}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    pages["bots.info"] = [{"ok": False, "error": "bot_not_found"}]
    client = FakeSlack(pages)
    ingest_workspace(conn, client, "gravitate", me=None)

    assert len([1 for method, _ in client.calls if method == "bots.info"]) == 1
    assert "B0GONE" in conn.execute("SELECT author_name FROM messages").fetchone()[0]


# --- Never-synced channels go first -----------------------------------------
#
# The tool failed its own motivating question: the 1:1 DM with Hunter Davis
# held 5,657 messages from 2022 onward and not one of them was in the corpus,
# while 59,390 messages from other channels were ingested across several runs.
# With 254 IMs and 1,382 group DMs all in tier 1 and no ordering at all, the
# same channels won every run and a channel in the tail was never reached --
# not slowly, never. A caught-up channel needs seconds; a never-synced one
# holds everything.

def _mark_current(conn, channel_id, *, when=1757000000.0):
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, last_ts, status, updated_at)"
        " VALUES ('gravitate', ?, 1757606400.000100, 'ok', ?)"
        " ON CONFLICT (workspace, channel_id) DO UPDATE SET"
        " last_ts = excluded.last_ts, status = 'ok', updated_at = excluded.updated_at",
        (channel_id, when),
    )
    conn.execute(
        "INSERT INTO backfill_state (workspace, channel_id, oldest_ts, complete, updated_at)"
        " VALUES ('gravitate', ?, 1757606400.000100, 1, ?)"
        " ON CONFLICT (workspace, channel_id) DO UPDATE SET complete = 1,"
        " updated_at = excluded.updated_at",
        (channel_id, when),
    )
    conn.commit()


def _dm_channels(ids):
    return [{"id": cid, "is_im": True, "user": "U03K2ABCD"} for cid in ids]


def test_a_never_synced_channel_is_reached_before_an_already_current_one(tmp_path):
    conn = open_db(tmp_path / "c.db")
    ids = ["D01CURRENT", "D02CURRENT", "D03NEVER"]
    histories = {
        "D01CURRENT": SlackHistory([]),
        "D02CURRENT": SlackHistory([]),
        "D03NEVER": SlackHistory([
            {"ts": f"{1700000000 + i}.000100", "user": "U03K2ABCD", "text": f"m{i}"}
            for i in range(50)
        ]),
    }
    client = client_serving(histories, channels=_dm_channels(ids))
    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0,
                     now=TickingClock())
    conn.execute("DELETE FROM messages")
    _mark_current(conn, "D01CURRENT")
    _mark_current(conn, "D02CURRENT")
    conn.execute(
        "DELETE FROM sync_state WHERE channel_id = 'D03NEVER'"
    )
    conn.execute("DELETE FROM backfill_state WHERE channel_id = 'D03NEVER'")
    conn.commit()

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=12.0,
                     now=TickingClock())

    assert conn.execute(
        "SELECT count(*) FROM messages WHERE channel_id = 'D03NEVER'"
    ).fetchone()[0] > 0


def test_every_channel_is_reached_within_a_bounded_number_of_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    ids = [f"D{i:02d}" for i in range(8)]
    histories = {
        cid: SlackHistory([{"ts": f"{1700000000 + i}.000100", "user": "U03K2ABCD",
                            "text": f"{cid}-m{i}"} for i in range(3)])
        for i, cid in enumerate(ids)
    }
    client = client_serving(histories, channels=_dm_channels(ids))

    clock = TickingClock()
    for _ in range(12):
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=20.0, now=clock)

    covered = {
        row[0]
        for row in conn.execute("SELECT DISTINCT channel_id FROM messages")
    }
    assert covered == set(ids)


def test_the_report_names_how_many_channels_have_never_been_synced(tmp_path):
    conn = open_db(tmp_path / "c.db")
    ids = ["D01", "D02", "D03"]
    histories = {cid: SlackHistory([]) for cid in ids}
    client = client_serving(histories, channels=_dm_channels(ids))

    report = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0,
                              now=TickingClock())
    assert report.channels_never_synced == 0

    # Two channels lose their state, and a spent budget still buys each tier
    # one channel -- so exactly one of them is reached and the other is still
    # waiting when the report is written.
    for channel_id in ("D02", "D03"):
        conn.execute("DELETE FROM sync_state WHERE channel_id = ?", (channel_id,))
        conn.execute("DELETE FROM backfill_state WHERE channel_id = ?", (channel_id,))
    conn.commit()
    report = ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=0.0,
                              now=TickingClock())
    assert report.channels_never_synced == 1


# --- 1:1 DMs are their own priority -----------------------------------------
#
# Tier 1 lumped `im` and `mpim` together, and Slack group-DM ids begin with C
# while 1:1 DM ids begin with D. With `id` as the tiebreak the alphabet
# guaranteed that all 844 never-synced group DMs went before the first 1:1 DM:
# 0 of 254 IMs ingested, including the DM holding 5,657 messages with the one
# person the motivating query names.

def _mixed_workspace(im_count: int, mpim_count: int):
    channels = [
        {"id": f"C{i:04d}GROUP", "is_mpim": True, "name": f"mpdm-{i}"}
        for i in range(mpim_count)
    ] + [
        {"id": f"D{i:04d}DIRECT", "is_im": True, "user": "U03K2ABCD"}
        for i in range(im_count)
    ]
    # The higher tiers carry real backlog, as they do live -- a 5,657-message
    # DM does not finish in one run. A fixture where they empty quickly lets
    # the lower tiers run on the leftovers and hides the bug entirely.
    histories = {
        channel["id"]: SlackHistory([
            {"ts": f"{1700000000 + i}.000100", "user": "U03K2ABCD",
             "text": f"{channel['id']}-m{i}"}
            for i in range(2 if "LURKED" in channel["id"] else 500)
        ])
        for channel in channels
    }
    client = client_serving(histories, channels=channels)
    client._pages["conversations.members"] = [
        {"ok": True, "members": ["U07JIMVOG", "U03K2ABCD"]}
    ]
    return client, channels


def test_a_one_to_one_dm_is_reached_in_the_first_run_despite_800_group_dms(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client, _ = _mixed_workspace(im_count=250, mpim_count=800)

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=200.0,
                     now=TickingClock())

    ims_with_messages = conn.execute(
        "SELECT count(DISTINCT m.channel_id) FROM messages m"
        " JOIN channels c ON c.workspace = m.workspace AND c.id = m.channel_id"
        " WHERE c.kind = 'im'"
    ).fetchone()[0]
    assert ims_with_messages > 0


def test_no_kind_is_permanently_starved_across_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client, channels = _mixed_workspace(im_count=12, mpim_count=30)

    clock = TickingClock()
    for _ in range(40):
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=60.0, now=clock)

    covered = {row[0] for row in conn.execute("SELECT DISTINCT channel_id FROM messages")}
    assert covered == {channel["id"] for channel in channels}


# --- Human threads are swept before bot threads ------------------------------
#
# The live backlog was 10,213 unswept threads concentrated in 9 channels, 87%
# of them under Jira, Sentry and PR-bot parents whose messages the chunker then
# excludes. At 25 per channel per run that is hours of API calls to fetch
# replies nobody will ever retrieve, while 1,078 human-parent threads -- actual
# conversation -- wait behind them in the same queue.

def _thread_fixture(conn, *, humans, bots, channel="C0ALERTS"):
    """Parents already stored and recorded as unswept, human ids after bot ids
    so a purely chronological order puts the humans last."""
    parents = []
    for index in range(bots):
        parents.append((f"{1700000000 + index}.000100", "B01T0JT986T", "Jira", "alert"))
    for index in range(humans):
        parents.append((f"{1700009000 + index}.000100", "U03K2ABCD", "Hunter Davis", "question"))
    for ts_raw, author_id, author, text in parents:
        conn.execute(
            "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
            " ts, ts_raw, thread_ts, thread_ts_raw, author_id, author_name, is_me, text,"
            " permalink, meta)"
            " VALUES (?, 'gravitate', ?, 'alerts', 'public', ?, ?, ?, ?, ?, ?, 0, ?, 'x', '{}')",
            (f"gravitate:{channel}:{ts_raw}", channel, float(ts_raw), ts_raw,
             float(ts_raw), ts_raw, author_id, author, text),
        )
        conn.execute(
            "INSERT INTO thread_sweeps (workspace, channel_id, thread_ts, thread_ts_raw,"
            " last_swept, swept) VALUES ('gravitate', ?, ?, ?, 0, 0)",
            (channel, float(ts_raw), ts_raw),
        )
    conn.commit()
    return [ts for ts, _, _, _ in parents]


def _sweep_client(parents, channel="C0ALERTS"):
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": channel, "name": "alerts", "is_channel": True, "is_member": True},
    ]}]
    pages["conversations.history"] = [{"ok": True, "messages": []}]
    client = FakeSlack(pages)
    serve_threads(client, {
        ts: [{"ts": ts, "user": "U03K2ABCD", "text": "parent", "thread_ts": ts}]
        for ts in parents
    })
    return client


def _swept(client):
    return [p["ts"] for m, p in client.calls if m == "conversations.replies"]


def test_a_human_parent_thread_is_swept_before_a_bot_parent_one(tmp_path):
    conn = open_db(tmp_path / "c.db")
    parents = _thread_fixture(conn, humans=2, bots=THREAD_SWEEP_PER_CHANNEL_CAP + 10)
    human_parents = parents[-2:]
    client = _sweep_client(parents)

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0,
                     now=lambda: 1700009100.0)

    swept = _swept(client)
    assert set(human_parents) <= set(swept)
    assert swept[:2] == human_parents


def test_bot_parent_threads_are_delayed_not_starved(tmp_path):
    conn = open_db(tmp_path / "c.db")
    parents = _thread_fixture(conn, humans=40, bots=10)
    bot_parents = set(parents[:10])
    client = _sweep_client(parents)

    clock = 1700009100.0
    swept_over_runs = set()
    for _ in range(6):
        client.calls.clear()
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0,
                         now=lambda: clock)
        swept_over_runs |= set(_swept(client))

    assert bot_parents <= swept_over_runs


def test_the_per_channel_cap_still_bounds_a_prioritised_sweep(tmp_path):
    conn = open_db(tmp_path / "c.db")
    parents = _thread_fixture(conn, humans=60, bots=60)
    client = _sweep_client(parents)

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=1000.0,
                     now=lambda: 1700009100.0)

    assert len(_swept(client)) <= THREAD_SWEEP_PER_CHANNEL_CAP


# --- The scheduler, at the scale it actually runs at --------------------------
#
# Three starvation bugs have now shipped from this scheduler: no ordering at
# all, then an alphabetical tiebreak that put every group DM ahead of every 1:1
# DM, then budget arithmetic that summed to 1.2x and left the last tier with a
# deadline already in the past. Every one of them was invisible to the tests
# because the fixtures had a handful of channels and a generous budget, which
# is the one regime where a broken scheduler looks fine. This is the fixture
# that regime hides from: several hundred channels of every kind, a budget that
# covers a fraction of them, and the only question that matters -- is every
# channel reached.

def _every_kind_workspace():
    channels = (
        [{"id": f"D{i:04d}DIRECT", "is_im": True, "user": "U03K2ABCD"} for i in range(40)]
        + [{"id": f"C{i:04d}GROUP", "is_mpim": True, "name": f"mpdm-{i}"} for i in range(60)]
        + [{"id": f"C{i:04d}SPOKEN", "name": f"spoken-{i}", "is_channel": True,
            "is_member": True} for i in range(30)]
        + [{"id": f"C{i:04d}LURKED", "name": f"lurked-{i}", "is_channel": True,
            "is_member": True} for i in range(70)]
    )
    # The higher tiers carry real backlog, as they do live -- a 5,657-message
    # DM does not finish in one run. A fixture where they empty quickly lets
    # the lower tiers run on the leftovers and hides the bug entirely.
    histories = {
        channel["id"]: SlackHistory([
            {"ts": f"{1700000000 + i}.000100", "user": "U03K2ABCD",
             "text": f"{channel['id']}-m{i}"}
            for i in range(2 if "LURKED" in channel["id"] else 500)
        ])
        for channel in channels
    }
    client = client_serving(histories, channels=channels)
    client._pages["conversations.members"] = [
        {"ok": True, "members": ["U07JIMVOG", "U03K2ABCD"]}
    ]
    return client, channels


def _make_tier_three(conn, channels):
    """A channel is tier 3 only if Jim has posted in it."""
    for channel in channels:
        if "SPOKEN" not in channel["id"]:
            continue
        conn.execute(
            "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
            " ts, ts_raw, thread_ts, thread_ts_raw, author_id, author_name, is_me, text,"
            " permalink, meta)"
            " VALUES (?, 'gravitate', ?, 'spoken', 'public', 1.0, '1.000000', NULL, '',"
            " 'U07JIMVOG', 'Jim Vogel', 1, 'mine', 'x', '{}')",
            (f"gravitate:{channel['id']}:1.000000", channel["id"]),
        )
    conn.commit()


def test_every_channel_of_every_kind_is_reached_under_a_tight_budget(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client, channels = _every_kind_workspace()
    _make_tier_three(conn, channels)

    # A budget that covers a fraction of the work, which is the regime the
    # earlier fixtures never put the scheduler in.
    clock = TickingClock()
    for _ in range(40):
        ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=60.0, now=clock)

    attempted = {
        row[0]
        for row in conn.execute(
            "SELECT channel_id FROM sync_state WHERE workspace = 'gravitate'"
            " AND attempted_at > 0"
        )
    }
    missed = {c["id"] for c in channels} - attempted
    assert not missed, f"{len(missed)} channels never attempted, e.g. {sorted(missed)[:5]}"

    covered = {row[0] for row in conn.execute("SELECT DISTINCT channel_id FROM messages")}
    assert {c["id"] for c in channels} <= covered


def test_the_lowest_tier_is_attempted_even_when_earlier_tiers_are_hungry(tmp_path):
    """The live symptom: #general, #random and 72 others sat at `never
    attempted` through seven runs and 538k messages, because the shares summed
    to 1.2 and tier 4's deadline was already in the past when it started."""
    conn = open_db(tmp_path / "c.db")
    client, channels = _every_kind_workspace()
    _make_tier_three(conn, channels)

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=60.0,
                     now=TickingClock())

    lurked = [c["id"] for c in channels if "LURKED" in c["id"]]
    attempted = {
        row[0]
        for row in conn.execute(
            "SELECT channel_id FROM sync_state WHERE attempted_at > 0"
        )
    }
    assert attempted & set(lurked)


def test_the_tier_shares_are_not_oversubscribed():
    """An edit that oversubscribes them must fail loudly, not silently starve
    whichever tier happens to run last."""
    assert sum(TIER_BUDGET_SHARE.values()) == pytest.approx(1.0)
    assert all(share > 0 for share in TIER_BUDGET_SHARE.values())


def test_a_tier_that_finishes_early_hands_its_time_onward(tmp_path):
    """Tier 1 has nothing to do, so tiers below it should get more than their
    nominal share rather than the run simply ending sooner."""
    conn = open_db(tmp_path / "c.db")
    client, channels = _every_kind_workspace()
    _make_tier_three(conn, channels)
    conn.execute("DELETE FROM channels")  # rebuilt by the directory walk
    conn.commit()

    only_lurked = [c for c in channels if "LURKED" in c["id"]]
    client._pages["conversations.list"] = [{"ok": True, "channels": only_lurked}]

    ingest_workspace(conn, client, "gravitate", me=None, budget_seconds=200.0,
                     now=TickingClock())

    attempted = conn.execute(
        "SELECT count(*) FROM sync_state WHERE attempted_at > 0"
    ).fetchone()[0]
    assert attempted > len(only_lurked) * TIER_BUDGET_SHARE[4]


# --- No bare IDs in anything an agent reads ----------------------------------
#
# 5,053 of 588,575 messages carried a raw id as the author name -- external and
# Slack Connect users in shared channels, plus deactivated accounts -- and 232
# chunks had one as a speaker. The spec is explicit: an agent searching for
# "Hunter" must not need to know he is U03K2. DM counterparties already got
# this treatment; the authorship path never did.

def _client_with_stranger(messages, *, user_info=None, bot_info=None):
    pages = dict(DIRECTORY)
    pages["conversations.history"] = [{"ok": True, "messages": messages}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    if user_info is not None:
        pages["users.info"] = [user_info]
    if bot_info is not None:
        pages["bots.info"] = [bot_info]
    return FakeSlack(pages)


def test_an_author_absent_from_the_user_list_is_resolved_by_lookup(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _client_with_stranger(
        [{"ts": "1757606400.000100", "user": "U07D4LWMWKW",
          "text": "I've been asked to review Eraser AI"}],
        user_info={"ok": True, "user": {"id": "U07D4LWMWKW", "name": "erin",
                                        "profile": {"real_name": "Erin Calder"}}},
    )
    ingest_workspace(conn, client, "gravitate", me=None)

    assert conn.execute("SELECT author_name FROM messages").fetchone()[0] == "Erin Calder"


def test_an_unresolvable_author_gets_a_marked_placeholder_not_a_bare_id(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _client_with_stranger(
        [{"ts": "1757606400.000100", "user": "U080JQRV29H", "text": "released a new tool"}],
        user_info={"ok": False, "error": "user_not_found"},
    )
    ingest_workspace(conn, client, "gravitate", me=None)

    name = conn.execute("SELECT author_name FROM messages").fetchone()[0]
    assert name != "U080JQRV29H"
    assert "U080JQRV29H" in name and "Unknown" in name


def test_an_unknown_author_is_looked_up_once_however_many_messages(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _client_with_stranger(
        [{"ts": f"175760640{i}.000100", "user": "U07D4LWMWKW", "text": f"m{i}"}
         for i in range(5)],
        user_info={"ok": True, "user": {"id": "U07D4LWMWKW", "name": "erin",
                                        "profile": {"real_name": "Erin Calder"}}},
    )
    ingest_workspace(conn, client, "gravitate", me=None)

    assert len([1 for method, _ in client.calls if method == "users.info"]) == 1


def test_a_bot_first_seen_as_a_message_author_resolves_through_bots_info(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _client_with_stranger(
        [{"ts": "1757606400.000100", "bot_id": "B01T0JT986T", "text": "GSD-9015 created"}],
        bot_info={"ok": True, "bot": {"id": "B01T0JT986T", "name": "Jira"}},
    )
    ingest_workspace(conn, client, "gravitate", me=None)

    assert conn.execute("SELECT author_name FROM messages").fetchone()[0] == "Jira"


def test_rows_already_stored_with_a_bare_id_are_repaired(tmp_path):
    """0.86% of a 588k-message corpus, ingested before the lookup existed.
    Repairing them from the users table costs one lookup per distinct author."""
    conn = open_db(tmp_path / "c.db")
    client = _client_with_stranger(
        [{"ts": "1757700000.000100", "user": "U03K2ABCD", "text": "hello"}],
        user_info={"ok": True, "user": {"id": "U07D4LWMWKW", "name": "erin",
                                        "profile": {"real_name": "Erin Calder"}}},
    )
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " ts_raw, thread_ts, thread_ts_raw, author_id, author_name, is_me, text, permalink, meta)"
        " VALUES ('gravitate:D1:1757606400.000100', 'gravitate', 'D1', 'Hunter Davis', 'im',"
        " 1757606400.0001, '1757606400.000100', NULL, '', 'U07D4LWMWKW', 'U07D4LWMWKW', 0,"
        " 'older message', 'x', '{}')"
    )
    conn.commit()

    ingest_workspace(conn, client, "gravitate", me=None)

    assert conn.execute(
        "SELECT author_name FROM messages WHERE text = 'older message'"
    ).fetchone()[0] == "Erin Calder"
    assert conn.execute(
        "SELECT needs_rechunk FROM sync_state WHERE channel_id = 'D1'"
    ).fetchone()[0] == 1


# --- A signal that can never go green is not a signal -------------------------
#
# The Slackbot DM fails `channel_not_found` on every run for the life of the
# corpus: conversations.history cannot read it at all. Counting it as an
# outstanding fault kept `status` non-zero permanently and put a warning on
# every read command that nothing could ever clear -- the same alarm fatigue
# the is_member fix removed for channels the user had left.

def _failing_client(error, channel="D0SLACKBOT"):
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": channel, "is_im": True, "user": "USLACKBOT"},
        {"id": "D1", "is_im": True, "user": "U03K2ABCD"},
    ]}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    client = FakeSlack(pages)
    original = client.call

    def call(method, **params):
        if method == "conversations.history" and params.get("channel") == channel:
            client.calls.append((method, params))
            raise SlackError(error)
        if method == "conversations.history":
            client.calls.append((method, params))
            return {"ok": True, "messages": []}
        return original(method, **params)

    client.call = call
    return client


def _history_calls_for(client, channel):
    return [p for m, p in client.calls if m == "conversations.history" and p.get("channel") == channel]


def test_a_permanently_unreachable_channel_is_recorded_as_unavailable(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _failing_client("channel_not_found")
    report = ingest_workspace(conn, client, "gravitate", me=None)

    assert conn.execute(
        "SELECT status FROM sync_state WHERE channel_id = 'D0SLACKBOT'"
    ).fetchone()[0] == "unavailable"
    assert report.channels_failed == []
    assert [c for c, _ in report.channels_unavailable] == ["D0SLACKBOT"]


def test_an_unavailable_channel_is_not_rescheduled_next_run(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _failing_client("channel_not_found")
    ingest_workspace(conn, client, "gravitate", me=None)

    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    assert _history_calls_for(client, "D0SLACKBOT") == []
    assert _history_calls_for(client, "D1")  # the rest of the workspace is unaffected


def test_a_transient_failure_is_retried_and_still_counts_as_a_fault(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _failing_client("ratelimited")
    report = ingest_workspace(conn, client, "gravitate", me=None)

    assert [c for c, _ in report.channels_failed] == ["D0SLACKBOT"]
    assert report.channels_unavailable == []

    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    assert _history_calls_for(client, "D0SLACKBOT")  # tried again


def test_an_unavailable_channel_is_re_checked_eventually(tmp_path):
    """Permanent as far as this run is concerned, not as far as forever is: a
    channel the user rejoins must find its way back in."""
    conn = open_db(tmp_path / "c.db")
    client = _failing_client("channel_not_found")
    ingest_workspace(conn, client, "gravitate", me=None)

    conn.execute(
        "UPDATE sync_state SET updated_at = updated_at - ?"
        " WHERE channel_id = 'D0SLACKBOT'",
        (UNAVAILABLE_RETRY_SECONDS + 1,),
    )
    conn.commit()
    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    assert _history_calls_for(client, "D0SLACKBOT")


# --- "Retrying cannot help" and "nobody can help" are different questions -----
#
# missing_scope and restricted_action will not fix themselves, so retrying them
# every run is pointless -- but the caller can fix them in two minutes, which
# makes them exactly the loud, actionable fault this project exists to surface.
# Setting them aside quietly would stop the corpus covering a whole class of
# channel while `status` read green.

def _blocked_client(error="missing_scope", detail="groups:history", channel="C0PRIVATE"):
    pages = dict(DIRECTORY)
    pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": channel, "name": "private-thing", "is_channel": True,
         "is_private": True, "is_member": True},
        {"id": "D1", "is_im": True, "user": "U03K2ABCD"},
    ]}]
    pages["conversations.replies"] = [{"ok": True, "messages": []}]
    client = FakeSlack(pages)
    original = client.call

    def call(method, **params):
        if method == "conversations.history":
            client.calls.append((method, params))
            if params.get("channel") == channel:
                raise SlackError(error, detail=detail)
            return {"ok": True, "messages": []}
        return original(method, **params)

    client.call = call
    return client


def test_a_missing_scope_is_recorded_as_blocked_and_names_the_scope(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _blocked_client()
    report = ingest_workspace(conn, client, "gravitate", me=None)

    row = conn.execute(
        "SELECT status, detail FROM sync_state WHERE channel_id = 'C0PRIVATE'"
    ).fetchone()
    assert row["status"] == "blocked"
    assert "groups:history" in row["detail"]
    assert [c for c, _ in report.channels_blocked] == ["C0PRIVATE"]
    assert report.channels_failed == []
    assert report.channels_unavailable == []


def test_a_blocked_channel_is_not_retried_every_run(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _blocked_client()
    ingest_workspace(conn, client, "gravitate", me=None)

    client.calls.clear()
    ingest_workspace(conn, client, "gravitate", me=None)
    assert _history_calls_for(client, "C0PRIVATE") == []
    assert _history_calls_for(client, "D1")
