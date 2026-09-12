"""Users and channels sync, name resolution, and backfill tier assignment.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_directory.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.directory import (
    MEMBERS_PER_RUN,
    Resolver,
    sync_directory,
    tier_for,
)
from sherpa.corpus.slack import FakeSlack, AuthError


def make_basic_client():
    return FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_H", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "C1", "name": "gravi-cli", "is_channel": True, "is_private": False, "is_member": True},
            {"id": "D1", "is_im": True, "user": "U_H"},
        ]}],
        "conversations.members": [{"ok": True, "members": []}],
    })


def test_sync_directory_stores_users_with_real_names(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_basic_client(), "gravitate", me=None)
    names = dict(conn.execute("SELECT id, name FROM users WHERE workspace='gravitate'"))
    assert names["U_H"] == "Hunter Reeves"


def test_sync_directory_returns_and_flags_me(tmp_path):
    conn = open_db(tmp_path / "c.db")
    assert sync_directory(conn, make_basic_client(), "gravitate", me=None) == "U_ME"
    assert conn.execute("SELECT is_me FROM users WHERE id='U_ME'").fetchone()[0] == 1


def test_dm_channel_is_named_for_the_other_person(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_basic_client(), "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    assert resolver.channel("D1") == ("Hunter Reeves", "im")
    assert resolver.channel("C1") == ("gravi-cli", "public")


def test_dm_with_unresolvable_counterparty_gets_marked_placeholder(tmp_path):
    """DM whose counterparty is unknown gets a marked placeholder, never a bare ID."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "D_UNKNOWN", "is_im": True, "user": "U_UNKNOWN"},
        ]}],
        "users.info": [{"ok": False, "error": "user_not_found"}],
        "conversations.members": [{"ok": True, "members": []}],
    })
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, client, "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    name, kind = resolver.channel("D_UNKNOWN")
    assert kind == "im"
    assert "Unknown user" in name
    assert "U_UNKNOWN" in name
    assert name != "U_UNKNOWN"  # Not a bare ID


def test_dm_resolved_via_users_info(tmp_path):
    """DM whose counterparty is absent from users.list is resolved via users.info."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "D_SLACK_CONNECT", "is_im": True, "user": "U_EXTERNAL"},
        ]}],
        "users.info": [{"ok": True, "user": {
            "id": "U_EXTERNAL", "name": "external", "profile": {"real_name": "External User"}
        }}],
        "conversations.members": [{"ok": True, "members": []}],
    })
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, client, "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    assert resolver.channel("D_SLACK_CONNECT") == ("External User", "im")
    assert conn.execute("SELECT name FROM users WHERE id='U_EXTERNAL'").fetchone()[0] == "External User"


def test_resolver_channel_returns_human_names_for_all_kinds(tmp_path):
    """Resolver.channel() returns human names, not IDs, for every kind."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_ALICE", "name": "alice", "profile": {"real_name": "Alice Smith"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "C_PUBLIC", "name": "public-channel", "is_channel": True, "is_private": False},
            {"id": "C_PRIVATE", "name": "private-channel", "is_channel": True, "is_private": True},
            {"id": "D_DM", "is_im": True, "user": "U_ALICE"},
            {"id": "G_MPIM", "is_mpim": True, "name": "mpdm-alice--1"},
        ]}],
        "conversations.members": [{"ok": True, "members": ["U_ME", "U_ALICE"]}],
    })
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, client, "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")

    assert resolver.channel("C_PUBLIC") == ("public-channel", "public")
    assert resolver.channel("C_PRIVATE") == ("private-channel", "private")
    assert resolver.channel("D_DM") == ("Alice Smith", "im")
    assert resolver.channel("G_MPIM") == ("Jim Vogel, Alice Smith", "mpim")


def test_mpim_named_from_resolved_participants(tmp_path):
    """MPIM is named from resolved participant names, not mpdm-...."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_ALICE", "name": "alice", "profile": {"real_name": "Alice Smith"}},
            {"id": "U_BOB", "name": "bob", "profile": {"real_name": "Bob Jones"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "G_GROUP", "is_mpim": True, "name": "mpdm-alice--bob-1"},
        ]}],
        "conversations.members": [{"ok": True, "members": ["U_ME", "U_ALICE", "U_BOB"]}],
    })
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, client, "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    name, kind = resolver.channel("G_GROUP")
    assert kind == "mpim"
    assert name == "Jim Vogel, Alice Smith, Bob Jones"
    assert name != "mpdm-alice--bob-1"


def test_member_ids_populated_for_im_and_mpim(tmp_path):
    """member_ids is populated for im and mpim, empty for public/private."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_ALICE", "name": "alice", "profile": {"real_name": "Alice Smith"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "C_PUBLIC", "name": "public", "is_channel": True, "is_private": False},
            {"id": "D_DM", "is_im": True, "user": "U_ALICE"},
            {"id": "G_MPIM", "is_mpim": True, "name": "group"},
        ]}],
        "conversations.members": [{"ok": True, "members": ["U_ME", "U_ALICE"]}],
    })
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, client, "gravitate", me=None)

    public_members = conn.execute("SELECT member_ids FROM channels WHERE id='C_PUBLIC'").fetchone()[0]
    assert public_members == "[]"

    dm_members = conn.execute("SELECT member_ids FROM channels WHERE id='D_DM'").fetchone()[0]
    assert dm_members == '["U_ME", "U_ALICE"]'

    mpim_members = conn.execute("SELECT member_ids FROM channels WHERE id='G_MPIM'").fetchone()[0]
    assert mpim_members == '["U_ME", "U_ALICE"]'


def test_autherror_from_users_info_propagates(tmp_path):
    """AuthError from users.info must propagate out of sync_directory, not be swallowed."""
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "D_EXTERNAL", "is_im": True, "user": "U_EXTERNAL"},
        ]}],
        "users.info": [{"ok": False, "error": "invalid_auth"}],
        "conversations.members": [{"ok": True, "members": []}],
    })
    conn = open_db(tmp_path / "c.db")
    with pytest.raises(AuthError):
        sync_directory(conn, client, "gravitate", me=None)


def test_autherror_from_conversations_members_propagates(tmp_path):
    """AuthError from conversations.members must propagate out of sync_directory.

    A group DM, because an IM no longer makes that call at all -- both of its
    parties are already known from the listing.
    """
    client = FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_ALICE", "name": "alice", "profile": {"real_name": "Alice Smith"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": "G_GROUP", "is_mpim": True, "name": "mpdm-alice-1"},
        ]}],
        "conversations.members": [{"ok": False, "error": "token_revoked"}],
    })
    conn = open_db(tmp_path / "c.db")
    with pytest.raises(AuthError):
        sync_directory(conn, client, "gravitate", me=None)


def test_tier_for_ranks_direct_dms_then_group_dms_then_posted_then_lurked():
    """A 1:1 DM outranks a group DM: 254 against 1,382, and it is where a
    promise to a named person lives."""
    assert tier_for({"kind": "im"}, has_posted=False) == 1
    assert tier_for({"kind": "mpim"}, has_posted=False) == 2
    assert tier_for({"kind": "public"}, has_posted=True) == 3
    assert tier_for({"kind": "public"}, has_posted=False) == 4


def test_a_channel_that_disappears_from_the_listing_stops_being_a_member(tmp_path):
    """A channel Jim leaves, or that gets archived, simply stops appearing in
    conversations.list. Left flagged as a member it is scheduled forever, fails
    forever, and `status` exits 2 permanently -- within weeks of normal churn
    the fail-loud signal is pure noise, which is how the corpus goes quietly
    incomplete by another route."""
    conn = open_db(tmp_path / "c.db")
    client = make_basic_client()
    sync_directory(conn, client, "gravitate", me=None)
    assert conn.execute("SELECT is_member FROM channels WHERE id='C1'").fetchone()[0] == 1

    client._pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": "D1", "is_im": True, "user": "U_H"},
    ]}]
    sync_directory(conn, client, "gravitate", me=None)

    assert conn.execute("SELECT is_member FROM channels WHERE id='C1'").fetchone()[0] == 0
    assert conn.execute("SELECT is_member FROM channels WHERE id='D1'").fetchone()[0] == 1


def test_departure_does_not_touch_another_workspace(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_basic_client(), "gravitate", me=None)
    conn.execute(
        "INSERT INTO channels (workspace, id, name, kind, is_member)"
        " VALUES ('other', 'C1', 'gravi-cli', 'public', 1)"
    )
    conn.commit()

    client = make_basic_client()
    client._pages["conversations.list"] = [{"ok": True, "channels": []}]
    sync_directory(conn, client, "gravitate", me=None)

    assert conn.execute(
        "SELECT is_member FROM channels WHERE workspace='other' AND id='C1'"
    ).fetchone()[0] == 1


# --- Membership is fetched incrementally ------------------------------------
#
# The real workspace has 254 IMs and 1,382 group DMs. One conversations.members
# call each, on every run, before the wall-clock budget is even computed, made a
# `--budget 120` sync take about seven minutes with not one message fetched --
# so `--budget` meant nothing and a 15-minute cron was impossible. Group DM
# membership is near-static; it does not need re-reading every quarter hour.

def _mpim_client(count: int, *, members=("U_ME", "U_H")):
    return FakeSlack({
        "auth.test": [{"ok": True, "user_id": "U_ME"}],
        "users.list": [{"ok": True, "members": [
            {"id": "U_ME", "name": "jim", "profile": {"real_name": "Jim Vogel"}},
            {"id": "U_H", "name": "hunter", "profile": {"real_name": "Hunter Reeves"}},
        ]}],
        "conversations.list": [{"ok": True, "channels": [
            {"id": f"G{i}", "is_mpim": True, "name": f"mpdm-jim--hunter-{i}"}
            for i in range(count)
        ]}],
        "conversations.members": [{"ok": True, "members": list(members)}],
    })


def _member_calls(client):
    return [p for m, p in client.calls if m == "conversations.members"]


def test_membership_is_not_refetched_on_every_run(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _mpim_client(1)
    sync_directory(conn, client, "gravitate", me=None)
    assert len(_member_calls(client)) == 1

    client.calls.clear()
    sync_directory(conn, client, "gravitate", me=None)
    assert _member_calls(client) == []


def test_a_newly_appeared_group_dm_has_its_membership_fetched(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _mpim_client(1)
    sync_directory(conn, client, "gravitate", me=None)

    client._pages["conversations.list"] = [{"ok": True, "channels": [
        {"id": "G0", "is_mpim": True, "name": "mpdm-jim--hunter-0"},
        {"id": "G_NEW", "is_mpim": True, "name": "mpdm-jim--hunter-new"},
    ]}]
    client.calls.clear()
    sync_directory(conn, client, "gravitate", me=None)

    assert [p["channel"] for p in _member_calls(client)] == ["G_NEW"]
    assert conn.execute("SELECT name FROM channels WHERE id='G_NEW'").fetchone()[0] == (
        "Jim Vogel, Hunter Reeves"
    )


def test_membership_refreshes_are_capped_per_run_and_finish_over_later_runs(tmp_path):
    conn = open_db(tmp_path / "c.db")
    total = MEMBERS_PER_RUN + 3
    client = _mpim_client(total)

    sync_directory(conn, client, "gravitate", me=None)
    assert len(_member_calls(client)) == MEMBERS_PER_RUN

    client.calls.clear()
    sync_directory(conn, client, "gravitate", me=None)
    assert len(_member_calls(client)) == 3

    unfetched = conn.execute(
        "SELECT count(*) FROM channels WHERE workspace='gravitate' AND members_fetched_at = 0"
    ).fetchone()[0]
    assert unfetched == 0


def test_a_dm_needs_no_members_call_at_all(tmp_path):
    """Both parties to an IM are already known: me, and the user the listing
    names. 254 requests a run for something already in hand."""
    conn = open_db(tmp_path / "c.db")
    client = make_basic_client()
    sync_directory(conn, client, "gravitate", me=None)

    assert _member_calls(client) == []
    assert conn.execute("SELECT member_ids FROM channels WHERE id='D1'").fetchone()[0] == (
        '["U_ME", "U_H"]'
    )


def test_directory_sync_stops_fetching_membership_at_its_deadline(tmp_path):
    conn = open_db(tmp_path / "c.db")
    client = _mpim_client(10)
    ticking = iter(range(1000))
    sync_directory(
        conn, client, "gravitate", me=None,
        now=lambda: float(next(ticking)), deadline=8.0,
    )
    assert 0 < len(_member_calls(client)) < 10
