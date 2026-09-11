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


def test_operations_declares_every_subcommand():
    import yaml

    meta = yaml.safe_load(corpus_tool.__doc__)
    assert meta["name"] == "corpus"
    assert meta["axi"] is True
    declared = set(meta["operations"])
    assert {"search", "with", "owed", "thread", "sync", "status"} == declared
    assert meta["operations"]["sync"]["tier"] == "write"
    assert meta["operations"]["search"]["tier"] == "read"
