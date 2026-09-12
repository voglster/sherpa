"""CLI contract: exit codes, TOON/JSON output, loud failures.

Run: uv run --with pytest --with httpx --with sqlite-vec --with python-toon==0.1.3 \
     pytest tests/test_corpus_cli.py -v
"""

from __future__ import annotations

import importlib.util
import json
import time
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
from sherpa.corpus.ingest import IngestReport, mark_dirty
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


def _seeded_db_with_many_chunks(path, count):
    """Several conversation chunks with Hunter Reeves, far enough apart (beyond
    chunk.GAP_SECONDS) that each pair of messages becomes its own chunk -- needed
    to exercise pagination, since a single continuous conversation collapses into
    one chunk regardless of how many messages it contains."""
    conn = open_db(path)
    for i in range(count):
        base = 10_000.0 + i * 5_000.0
        _insert_message(conn, base, "Hunter Reeves", f"message {i}")
        _insert_message(conn, base + 60.0, "Jim Vogel", f"reply {i}", is_me=1)
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
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"]


def test_stale_ok_suppresses_the_warning(workspace, capsys, monkeypatch):
    config, _ = workspace
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: _StubEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "anything", "--config", str(config), "--stale-ok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload


def test_search_returns_hits_and_exits_0(workspace, capsys, monkeypatch, tmp_path):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["search", "flag work", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 1
    assert "Hunter" in payload["results"][0]["conversation"] or "Jim" in payload["results"][0]["conversation"]


def test_search_with_no_matches_reports_a_definitive_empty_state(workspace, capsys, monkeypatch):
    config, _ = workspace
    open_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
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

    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: _BrokenEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["search", "anything", "--config", str(config), "--stale-ok"])
    assert caught.value.code == 1
    assert "gateway down" in capsys.readouterr().out


def test_make_embedder_reads_the_given_vault_path_not_the_real_one(tmp_path, monkeypatch):
    """`make_embedder` must honor an explicit vault path rather than always reading
    ~/.sherpa/vault.json -- otherwise `--vault` on a read subcommand is decorative and
    the CLI's embedding key resolution silently depends on whatever happens to be on
    the machine running it.
    """
    fake_home = tmp_path / "not-home"
    fake_home.mkdir()
    monkeypatch.setattr(corpus_tool.Path, "home", lambda: fake_home)

    given_vault = tmp_path / "given.json"
    given_vault.write_text(json.dumps({"LITELLM_KEY_CORPUS": "given-key"}))

    embedder = corpus_tool.make_embedder(given_vault)
    assert embedder._key == "given-key"


def test_search_missing_embedding_secret_reports_on_stderr_and_exits_2(workspace, tmp_path, capsys, monkeypatch):
    """Hermeticity guard: a read subcommand must resolve `LITELLM_KEY_CORPUS` from the
    `--vault` it was given, never the real `~/.sherpa/vault.json` -- otherwise this test's
    outcome would depend on ambient state on whatever machine runs the suite. `make_embedder`
    is deliberately NOT monkeypatched here so the real vault-reading code path runs.

    The fake "real" vault below deliberately DOES carry a valid-looking
    `LITELLM_KEY_CORPUS`, so this only passes if `--vault` actually overrides it -- a build
    that silently falls back to `~/.sherpa/vault.json` would find that key and succeed
    instead of reporting the secret as missing.
    """
    config, _ = workspace
    open_db(config.parent / "c.db").close()

    fake_home = tmp_path / "not-home"
    (fake_home / ".sherpa").mkdir(parents=True)
    (fake_home / ".sherpa" / "vault.json").write_text(
        json.dumps({"LITELLM_KEY_CORPUS": "should-not-be-used"})
    )
    monkeypatch.setattr(corpus_tool.Path, "home", lambda: fake_home)

    given_vault = tmp_path / "given.json"
    given_vault.write_text(json.dumps({"SLACK_USER_TOKEN_GRAVITATE": "xoxp-x"}))

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["search", "anything", "--config", str(config), "--vault", str(given_vault), "--stale-ok"]
        )
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "MISSING_SECRET: LITELLM_KEY_CORPUS" in captured.err
    assert captured.out.startswith("error:")


def test_with_returns_conversation_and_owed_filters_to_my_messages(workspace, capsys):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["with", "Hunter Reeves", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    with_payload = json.loads(capsys.readouterr().out)
    assert with_payload["count"] >= 1
    assert with_payload["total"] == with_payload["count"]

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["owed", "Hunter Reeves", "--config", str(config), "--stale-ok", "--json"])
    assert caught.value.code == 0
    owed_payload = json.loads(capsys.readouterr().out)
    assert owed_payload["count"] >= 1
    assert owed_payload["total"] == owed_payload["count"]
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
    assert payload["total"] == 0
    assert "Nobody Ever" in payload["note"]


def test_owed_reports_the_true_total_when_capped_and_limit_expands_it(workspace, capsys):
    config, _ = workspace
    _seeded_db_with_many_chunks(config.parent / "c.db", 5).close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["owed", "Hunter Reeves", "--limit", "2", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    capped = json.loads(capsys.readouterr().out)
    assert capped["count"] == 2
    assert capped["total"] == 5
    assert capped["help"] == ['sherpa corpus owed "Hunter Reeves" --limit 5']

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["owed", "Hunter Reeves", "--limit", "5", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    full = json.loads(capsys.readouterr().out)
    assert full["count"] == 5
    assert full["total"] == 5
    assert "help" not in full


def test_with_reports_the_true_total_when_capped_and_limit_expands_it(workspace, capsys):
    config, _ = workspace
    _seeded_db_with_many_chunks(config.parent / "c.db", 5).close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["with", "Hunter Reeves", "--limit", "2", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    capped = json.loads(capsys.readouterr().out)
    assert capped["count"] == 2
    assert capped["total"] == 5

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["with", "Hunter Reeves", "--limit", "5", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    full = json.loads(capsys.readouterr().out)
    assert full["count"] == 5
    assert full["total"] == 5


@pytest.mark.parametrize("command", ["with", "owed"])
@pytest.mark.parametrize("bad_limit", ["0", "-1"])
def test_limit_below_one_is_rejected_not_clamped(workspace, capsys, command, bad_limit):
    """`--limit 0` must not report a false empty state, and `--limit -1` must not
    silently defeat the cap (SQLite treats a negative LIMIT as no limit at all) --
    both are the exact "confident, wrong answer" failure mode this project exists
    to prevent, so both must be rejected loudly (exit 2) rather than clamped.
    """
    config, _ = workspace
    _seeded_db_with_many_chunks(config.parent / "c.db", 5).close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            [command, "Hunter Reeves", "--limit", bad_limit, "--config", str(config), "--stale-ok"]
        )
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "help:" in out
    assert "--limit" in out


@pytest.mark.parametrize("command", ["with", "owed"])
def test_limit_one_still_works(workspace, capsys, command):
    config, _ = workspace
    _seeded_db_with_many_chunks(config.parent / "c.db", 5).close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            [command, "Hunter Reeves", "--limit", "1", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["total"] == 5


@pytest.mark.parametrize("bad_k", ["0", "-1", "-5"])
def test_search_k_below_one_is_rejected(workspace, capsys, bad_k):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["search", "flag work", "--k", bad_k, "--config", str(config), "--stale-ok"]
        )
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "help:" in out
    assert "--k" in out


@pytest.mark.parametrize("bad_context", ["-1", "-100"])
def test_search_context_below_zero_is_rejected(workspace, capsys, bad_context):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["search", "flag work", "--context", bad_context, "--config", str(config), "--stale-ok"]
        )
    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "help:" in out
    assert "--context" in out


def test_search_context_zero_is_allowed(workspace, capsys, monkeypatch):
    """`--context 0` is a legitimate request (no surrounding messages), unlike a
    negative value -- it must not be rejected."""
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["search", "flag work", "--context", "0", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0


def test_search_k_one_still_works(workspace, capsys, monkeypatch):
    config, _ = workspace
    _seeded_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(
            ["search", "flag work", "--k", "1", "--config", str(config), "--stale-ok", "--json"]
        )
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1


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
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())

    def _fake_ingest(conn, client, name, me, budget_seconds):
        _insert_message(conn, 1000.0, "Hunter Reeves", "can you take the flag work?")
        _insert_message(conn, 1060.0, "Jim Vogel", "yeah I'll have it thursday", is_me=1)
        mark_dirty(conn, name, "D1")
        conn.commit()
        return IngestReport(
            workspace=name, messages=2, channels_done=1, channels_failed=[],
            channels_touched={"D1"},
        )

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
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())

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


def test_sync_rechunks_only_the_channels_that_received_messages(workspace, monkeypatch, capsys):
    """Re-windowing every channel on every cron run fully materializes the whole
    workspace -- 500k messages at the design's sizing -- and reissues a delete
    against chunks_fts for each changed window, all inside the wall-clock budget
    the tiered ingest exists to respect. Only channels that actually took
    messages this run need rebuilding."""
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())

    conn = open_db(config.parent / "c.db")
    _insert_message(conn, 1757606400.000100, "Hunter Reeves", "untouched channel",
                    channel="DOLD", name="Hunter Reeves")
    conn.commit()
    conn.close()

    def _fake_ingest(conn, client, name, me, budget_seconds):
        _insert_message(conn, 1757606500.000100, "Hunter Reeves", "can you take the flag work?")
        _insert_message(conn, 1757606560.000100, "Jim Vogel", "yeah I'll have it thursday", is_me=1)
        mark_dirty(conn, name, "D1")
        conn.commit()
        return IngestReport(
            workspace=name, messages=2, channels_done=1, channels_touched={"D1"}
        )

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _fake_ingest)

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    assert caught.value.code == 0

    conn = open_db(config.parent / "c.db")
    chunked = {row[0] for row in conn.execute("SELECT DISTINCT channel_id FROM chunks")}
    assert chunked == {"D1"}


def test_sync_reports_failed_threads_and_exits_1(workspace, monkeypatch, capsys):
    """A thread that cannot be read is not fatal to its channel, but it is
    still a hole in the corpus and must not pass unnoticed."""
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    monkeypatch.setattr(
        corpus_tool,
        "ingest_workspace",
        lambda *a, **k: IngestReport(
            workspace="gravitate",
            messages=1,
            channels_done=1,
            threads_failed=[("C1", "1757606400.000100", "ratelimited")],
        ),
    )

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    assert caught.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced"][0]["threads_failed"] == 1


def test_messages_committed_by_an_aborted_run_are_chunked_by_the_next_one(
    workspace, monkeypatch, capsys
):
    """Ingest commits messages page by page, but sync only rebuilds chunks after
    ingest_workspace returns -- and a rejected token, a rate-limit exhaustion or
    a kill all leave through paths that never reach it. A dirty set held in
    memory dies with the process, so those messages have no chunk, no fts row
    and no vector, forever: invisible to every read command, while `status`
    reports healthy because it counts only unembedded chunks and there are
    none. The next successful run has to pick them up."""
    config, vault = workspace
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())

    def _abort_after_committing(conn, client, name, me, budget_seconds):
        conn.execute(
            "INSERT INTO sync_state (workspace, channel_id, tier) VALUES (?, 'D1', 1)"
            " ON CONFLICT (workspace, channel_id) DO NOTHING",
            (name,),
        )
        _insert_message(conn, 1757606400.000100, "Jim Vogel",
                        "I owe Hunter the schema", is_me=1)
        mark_dirty(conn, name, "D1")
        conn.commit()
        raise AuthError("token_revoked")

    monkeypatch.setattr(corpus_tool, "ingest_workspace", _abort_after_committing)
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault)])
    assert caught.value.code == 2
    capsys.readouterr()

    conn = open_db(config.parent / "c.db")
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    conn.close()

    # A fresh process: nothing survives but the database.
    monkeypatch.setattr(
        corpus_tool,
        "ingest_workspace",
        lambda conn, client, name, me, budget_seconds: IngestReport(workspace=name),
    )
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    assert caught.value.code == 0

    conn = open_db(config.parent / "c.db")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedded = 1").fetchone()[0] == 1


def test_a_first_backfill_is_visible_without_being_an_error(workspace, tmp_path, capsys):
    """During the first cold backfill -- days, and exactly when the corpus is
    least complete -- nothing said so: staleness, unembedded chunks and failed
    channels were all clean, so `status` was silent and `search` printed no
    warning. An agent asking "did I miss anything?" got a confident partial
    answer.

    It warns, but it does not exit non-zero: a backfill in progress is not
    something the caller can fix, and exit 2 means "change something and
    retry". Days of that trains everyone to ignore the signal."""
    import time

    config, _ = workspace
    conn = open_db(config.parent / "c.db")
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, status, updated_at)"
        " VALUES ('gravitate', 'D1', 'ok', ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO backfill_state (workspace, channel_id, oldest_ts, complete)"
        " VALUES ('gravitate', 'D1', 1757606400.000100, 0)"
    )
    conn.execute(
        "INSERT INTO thread_sweeps (workspace, channel_id, thread_ts, thread_ts_raw, swept)"
        " VALUES ('gravitate', 'D1', 1757606400.0001, '1757606400.000100', 0)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert caught.value.code == 0
    assert any("backfill" in line for line in payload["warnings"])
    assert any("thread" in line for line in payload["warnings"])
    assert payload["workspaces"][0]["backfilling"] == 1
    assert payload["workspaces"][0]["unswept_threads"] == 1


def test_a_failed_channel_during_a_backfill_still_exits_non_zero(workspace, capsys):
    """The faults that do have remedies keep their teeth."""
    import time

    config, _ = workspace
    conn = open_db(config.parent / "c.db")
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, status, updated_at)"
        " VALUES ('gravitate', 'D1', 'failed', ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO backfill_state (workspace, channel_id, complete) VALUES ('gravitate', 'D1', 0)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["status", "--config", str(config), "--json"])
    capsys.readouterr()
    assert caught.value.code == 2


def test_search_still_warns_while_a_backfill_is_running(workspace, capsys, monkeypatch):
    """The confident-incompleteness guard on the read path is unchanged: a
    query answered from a corpus still reading its own history must say so."""
    import time

    config, _ = workspace
    conn = open_db(config.parent / "c.db")
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, status, updated_at)"
        " VALUES ('gravitate', 'D1', 'ok', ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO backfill_state (workspace, channel_id, complete) VALUES ('gravitate', 'D1', 0)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    with pytest.raises(SystemExit):
        corpus_tool.main(["search", "schema", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert any("backfill" in line for line in payload["warnings"])


def test_status_warns_about_channels_never_synced_and_stops_once_covered(workspace, capsys):
    config, _ = workspace
    conn = open_db(config.parent / "c.db")
    conn.execute(
        "INSERT INTO channels (workspace, id, name, kind, is_member)"
        " VALUES ('gravitate', 'D03G5S683V4', 'Hunter Davis', 'im', 1)"
    )
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, status, updated_at)"
        " VALUES ('gravitate', 'D03G5S683V4', 'pending', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspaces"][0]["never_synced"] == 1
    assert any("never" in line for line in payload["warnings"])

    conn = open_db(config.parent / "c.db")
    conn.execute(
        "UPDATE sync_state SET last_ts = 1757606400.000100, status = 'ok'"
        " WHERE channel_id = 'D03G5S683V4'"
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspaces"][0]["never_synced"] == 0
    assert not any("never" in line for line in payload.get("warnings", []))


def test_a_second_sync_steps_aside_instead_of_colliding(workspace, monkeypatch, capsys):
    """A cron run that overruns its interval meets the next one. That is
    expected operational behavior, not a fault -- and certainly not a raw
    `database is locked` traceback."""
    config, vault = workspace
    open_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())

    ran = []
    monkeypatch.setattr(
        corpus_tool,
        "ingest_workspace",
        lambda *a, **k: ran.append(1) or IngestReport(workspace="gravitate"),
    )

    held = corpus_tool.sync_lock(config.parent / "c.db")
    assert held is not None
    try:
        with pytest.raises(SystemExit) as caught:
            corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    finally:
        held.close()

    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "already running" in payload["note"]
    assert ran == []


def test_sync_runs_normally_when_no_other_holds_the_lock(workspace, monkeypatch, capsys):
    config, vault = workspace
    open_db(config.parent / "c.db").close()
    monkeypatch.setattr(corpus_tool, "notify", lambda *a, **k: None)
    monkeypatch.setattr(corpus_tool, "HttpSlackClient", lambda token: object())
    monkeypatch.setattr(corpus_tool, "make_embedder", lambda vault=None: FakeEmbedder())
    monkeypatch.setattr(
        corpus_tool, "ingest_workspace", lambda *a, **k: IngestReport(workspace="gravitate")
    )

    with pytest.raises(SystemExit) as caught:
        corpus_tool.main(["sync", "--config", str(config), "--vault", str(vault), "--json"])
    assert caught.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced"][0]["workspace"] == "gravitate"


def test_status_separates_the_human_thread_backlog_from_the_bot_one(workspace, capsys):
    """10,213 outstanding threads of which 1,078 have a human parent is a very
    different statement about completeness than one number can make."""
    config, _ = workspace
    conn = open_db(config.parent / "c.db")
    conn.execute(
        "INSERT INTO sync_state (workspace, channel_id, status, updated_at)"
        " VALUES ('gravitate', 'C0ALERTS', 'ok', ?)",
        (time.time(),),
    )
    for ts_raw, author_id in (("1700000000.000100", "B01T0JT986T"),
                              ("1700000001.000100", "B01T0JT986T"),
                              ("1700009000.000100", "U03K2ABCD")):
        conn.execute(
            "INSERT INTO messages (id, workspace, channel_id, channel_name, channel_kind,"
            " ts, ts_raw, thread_ts, thread_ts_raw, author_id, author_name, is_me, text,"
            " permalink, meta)"
            " VALUES (?, 'gravitate', 'C0ALERTS', 'alerts', 'public', ?, ?, ?, ?, ?, 'x', 0,"
            " 'parent', 'x', '{}')",
            (f"gravitate:C0ALERTS:{ts_raw}", float(ts_raw), ts_raw, float(ts_raw), ts_raw,
             author_id),
        )
        conn.execute(
            "INSERT INTO thread_sweeps (workspace, channel_id, thread_ts, thread_ts_raw, swept)"
            " VALUES ('gravitate', 'C0ALERTS', ?, ?, 0)",
            (float(ts_raw), ts_raw),
        )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        corpus_tool.main(["status", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspaces"][0]["unswept_threads"] == 3
    assert payload["workspaces"][0]["unswept_human_threads"] == 1
    assert any("1 with a human parent" in line for line in payload["warnings"])
