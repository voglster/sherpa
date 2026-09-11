"""Users and channels sync, name resolution, and backfill tier assignment.

Run: uv run --with pytest --with sqlite-vec pytest tests/test_corpus_directory.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.db import open_db
from sherpa.corpus.directory import Resolver, sync_directory, tier_for
from sherpa.corpus.slack import FakeSlack


def make_client():
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
    })


def test_sync_directory_stores_users_with_real_names(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    names = dict(conn.execute("SELECT id, name FROM users WHERE workspace='gravitate'"))
    assert names["U_H"] == "Hunter Reeves"


def test_sync_directory_returns_and_flags_me(tmp_path):
    conn = open_db(tmp_path / "c.db")
    assert sync_directory(conn, make_client(), "gravitate", me=None) == "U_ME"
    assert conn.execute("SELECT is_me FROM users WHERE id='U_ME'").fetchone()[0] == 1


def test_dm_channel_is_named_for_the_other_person(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    resolver = Resolver(conn, "gravitate")
    assert resolver.channel("D1") == ("Hunter Reeves", "im")
    assert resolver.channel("C1") == ("gravi-cli", "public")


def test_resolver_falls_back_to_the_raw_id_for_unknown_users(tmp_path):
    conn = open_db(tmp_path / "c.db")
    sync_directory(conn, make_client(), "gravitate", me=None)
    assert Resolver(conn, "gravitate").user_name("U_GHOST") == "U_GHOST"


def test_tier_for_ranks_dms_then_posted_then_lurked():
    assert tier_for({"kind": "im"}, has_posted=False) == 1
    assert tier_for({"kind": "mpim"}, has_posted=False) == 1
    assert tier_for({"kind": "public"}, has_posted=True) == 2
    assert tier_for({"kind": "public"}, has_posted=False) == 3
