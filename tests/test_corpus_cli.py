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

from sherpa.corpus.db import open_db
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


@pytest.mark.parametrize("command,positional", [
    ("search", "foo"),
    ("with", "alice"),
    ("owed", "alice"),
    ("thread", "https://x.slack.com/archives/C1/p1"),
])
def test_unimplemented_subcommand_exits_2_not_1(workspace, command, positional, capsys):
    """Retrying an unimplemented subcommand can never succeed, so it must be 2, not 1.

    Exit 1 means "the world was uncooperative, retry is reasonable" per
    docs/SHERPA_STANDARDS.md; an agent that saw 1 here would retry a command
    that cannot ever succeed until Task 10 lands. Checking only "it failed"
    would not catch a regression to the wrong exit code.
    """
    config, _ = workspace
    with pytest.raises(SystemExit) as caught:
        corpus_tool.main([command, positional, "--config", str(config)])
    assert caught.value.code == 2
    assert capsys.readouterr().out.startswith("error:")


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
