"""CLI contract: exit codes, TOON/JSON output, loud failures.

Run: uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 \
     pytest tests/test_corpus_cli.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_SPEC = importlib.util.spec_from_file_location("corpus_tool", ROOT / "tools" / "corpus.py")
corpus_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus_tool)

from sherpa.corpus.chunk import build_chunks
from sherpa.corpus.db import open_db
from sherpa.corpus.embed import EmbeddingError, FakeEmbedder
from sherpa.corpus.ingest import IngestReport
from sherpa.corpus.slack import AuthError, SlackError


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / "corpus.toml"
    config.write_text(
        f'db = "{tmp_path / "c.db"}"\n\n[workspace.gravitate]\n'
        'token_key = "SLACK_USER_TOKEN_GRAVITATE"\n'
    )
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"SLACK_USER_TOKEN_GRAVITATE": "xoxp-x"}))
    return config, vault


def test_unknown_flag_exits_2(workspace, capsys):
    config, _ = workspace
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--nonsense", "--config", str(config)])
    assert caught.value.code == 2
    assert "nonsense" in capsys.readouterr().out


def test_missing_config_exits_2_with_help(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(tmp_path / "absent.toml")])
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "help:" in out


def test_missing_secret_reports_on_stderr_and_exits_2(workspace, tmp_path, capsys):
    config, _ = workspace
    empty_vault = tmp_path / "empty.json"
    empty_vault.write_text("{}")
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(empty_vault)])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "MISSING_SECRET: SLACK_USER_TOKEN_GRAVITATE" in captured.err
    assert captured.out.startswith("error:")


def test_status_emits_json_when_asked(workspace, capsys):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "workspaces" in payload
    assert caught.value.code == 2  # never synced is not healthy


def _insert_message(conn, ts, author, text, *, is_me=0, channel="D1", kind="im", name="Hunter Reeves"):
    conn.execute(
        "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind, ts,"
        " author_id, author_name, is_me, text, permalink)"
        " VALUES (?, 'gravitate', ?, ?, ?, ?, 'U', ?, ?, ?, ?)",
        (
            f"gravitate:{channel}:{ts}",
            channel,
            name,
            kind,
            ts,
            author,
            is_me,
            text,
            f"https://gravitate.slack.com/archives/{channel}/p{int(ts)}",
        ),
    )


def _seeded_db(path):
    conn = open_db(path)
    _insert_message(conn, 1000.0, "Hunter Reeves", "can you take the flag work?")
    _insert_message(conn, 1060.0, "Jim Vogel", "yeah I'll have it thursday", is_me=1)
    conn.commit()
    build_chunks(conn, "gravitate", "D1")
    from sherpa.corpus.embed import embed_pending

    embed_pending(conn, FakeEmbedder())
    return conn


def test_parse_since_accepts_relative_and_iso_forms():
    import time

    now = time.time()
    assert abs(corpus_tool.parse_since("1d") - (now - 86400)) < 5
    assert abs(corpus_tool.parse_since("2h") - (now - 7200)) < 5
    assert abs(corpus_tool.parse_since("1w") - (now - 604800)) < 5
    assert corpus_tool.parse_since("2026-01-01") < now


def test_parse_since_rejects_garbage():
    with pytest.raises(SystemExit) as caught:
        corpus_tool.parse_since("soonish")
    assert caught.value.code == 2


class _StubEmbedder:
    def embed(self, texts):
        return [[0.0] * 768 for _ in texts]


def test_search_warns_loudly_when_the_corpus_is_stale(workspace, capsys, monkeypatch):
    config, _ = workspace
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"]


def test_stale_ok_suppresses_the_warning(workspace, capsys, monkeypatch):
    config, _ = workspace
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--stale-ok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


def test_search_returns_hits_and_exits_0(workspace, capsys, monkeypatch, tmp_path):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: FakeEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["search", "flag work", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 1
    assert "Hunter" in payload["results"][0]["conversation"] or "Jim" in payload["results"][0]["conversation"]


def test_search_with_no_matches_reports_a_definitive_empty_state(workspace, capsys, monkeypatch):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: FakeEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["search", "nothing here", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["results"] == []
    assert "note" in payload


def test_search_embedding_error_notifies_and_exits_1(workspace, capsys, monkeypatch):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)

    class _BrokenEmbedder:
        def embed(self, texts):
            raise EmbeddingError("gateway down")

    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: _BrokenEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["search", "anything", "--config", str(config), "--stale-ok"])
    assert caught.value.code == 1
    assert "gateway down" in capsys.readouterr().out


def test_with_returns_conversation_and_owed_filters_to_my_messages(workspace, capsys):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["with", "Hunter Reeves", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    with_payload = json.loads(capsys.readouterr().out)
    assert with_payload["count"] >= 1

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["owed", "Hunter Reeves", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    owed_payload = json.loads(capsys.readouterr().out)
    assert owed_payload["count"] >= 1
    assert "filter" in owed_payload["note"].lower() or "summarize" in owed_payload["note"].lower()
    assert all("Jim Vogel" in r["conversation"] for r in owed_payload["results"])


def test_owed_with_no_conversation_reports_a_definitive_empty_state(workspace, capsys):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["owed", "Nobody Ever", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert "Nobody Ever" in payload["note"]


def test_thread_at_known_permalink_returns_a_result(workspace, capsys):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    permalink = "https://gravitate.slack.com/archives/D1/p1000"
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["thread", permalink, "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["permalink"] == permalink


def test_thread_at_unknown_permalink_exits_1_with_help(workspace, capsys):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["thread", "https://gravitate.slack.com/archives/D1/pNOPE", "--config", str(config), "--stale-ok"]
        )
    assert caught.value.code == 1
    assert capsys.readouterr().out.startswith("error:")


def test_sync_also_chunks_and_embeds(workspace, monkeypatch, capsys):
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: FakeEmbedder())

    def _fake_ingest(conn, client, name, me, budget_seconds):
        _insert_message(conn, 1000.0, "Hunter Reeves", "can you take the flag work?")
        _insert_message(conn, 1060.0, "Jim Vogel", "yeah I'll have it thursday", is_me=1)
        conn.commit()
        return IngestReport(workspace=name, messages=2, channels_done=1, channels_failed=[])

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _fake_ingest)

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced"][0]["embedded"] >= 1


def test_sync_exits_2_when_token_is_rejected(workspace, monkeypatch, capsys):
    """A rejected token means retrying identically will never work: exit 2."""
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())

    def _raise_auth_error(*args, **kwargs):
        raise AuthError("invalid_auth")

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _raise_auth_error)

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault)])
    assert caught.value.code == 2
    assert capsys.readouterr().out.startswith("error:")


def test_sync_exits_1_when_some_channels_merely_fail(workspace, monkeypatch, capsys):
    """A channel-level hiccup is the world being uncooperative: exit 1, not 2."""
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda: FakeEmbedder())

    def _partial_failure(*args, **kwargs):
        return IngestReport(
            workspace="gravitate",
            messages=3,
            channels_done=2,
            channels_failed=[("C1", "http_503")],
        )

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _partial_failure)

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault)])
    assert caught.value.code == 1


def test_sync_exits_1_with_structured_error_when_slack_error_is_not_auth(workspace, monkeypatch, capsys):
    """A bare SlackError (e.g. ratelimit exhaustion, cursor repeat) must not become a traceback.

    `ingest_workspace` can raise a plain `SlackError` before it ever reaches its
    own per-channel try/except (e.g. from `sync_directory`'s `auth.test` or
    `paginate` calls). That is the world being uncooperative, not a bad token,
    so it must land as a structured `error:`/`help:` pair on stdout and exit 1
    — never a bare traceback on stderr, and never exit 2 (only a rejected
    token is exit 2).
    """
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())

    def _raise_slack_error(*args, **kwargs):
        raise SlackError("ratelimited", retryable=True)

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _raise_slack_error)

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault)])
    assert caught.value.code == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "ratelimited" in out
    assert "help:" in out


def test_operations_declares_every_subcommand():
    import yaml

    meta = yaml.safe_load(corpus_tool.__doc__)
    assert meta["name"] == "corpus"
    assert meta["axi"] is True
    declared = set(meta["operations"])
    assert {"search", "with", "owed", "thread", "sync", "status"} == declared
    assert meta["operations"]["sync"]["tier"] == "write"
    assert meta["operations"]["search"]["tier"] == "read"
