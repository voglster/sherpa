"""Config parsing and vault token resolution.

Run: uv run --with pytest pytest tests/test_corpus_config.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.config import ConfigError, MissingSecret, load_config, resolve_token

CONFIG = """
db = "~/.sherpa/corpus.db"
stale_after_hours = 24

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN_GRAVITATE"
me = "U03ABC"

[workspace.side]
token_key = "SLACK_USER_TOKEN_SIDE"
"""

CONFIG_CUSTOM_DB = """
db = "~/somewhere-else/corpus.db"
stale_after_hours = 24

[workspace.gravitate]
token_key = "SLACK_USER_TOKEN_GRAVITATE"
me = "U03ABC"

[workspace.side]
token_key = "SLACK_USER_TOKEN_SIDE"
"""


def test_load_config_reads_workspaces_in_order(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text(CONFIG)
    config = load_config(path)
    assert [w.name for w in config.workspaces] == ["gravitate", "side"]
    assert config.workspaces[0].token_key == "SLACK_USER_TOKEN_GRAVITATE"
    assert config.workspaces[0].me == "U03ABC"
    assert config.workspaces[1].me is None
    assert config.stale_after_hours == 24


def test_load_config_expands_db_path(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text(CONFIG_CUSTOM_DB)
    assert load_config(path).db == Path.home() / "somewhere-else" / "corpus.db"
    # Also verify ~ is expanded
    assert "~" not in str(load_config(path).db)


def test_missing_config_raises_with_actionable_help(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path / "absent.toml")
    assert "corpus.toml" in caught.value.message
    assert caught.value.help


def test_workspace_without_token_key_is_rejected(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text('db = "x.db"\n\n[workspace.broken]\nme = "U1"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_resolve_token_reads_the_vault(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"SLACK_USER_TOKEN_GRAVITATE": "xoxp-real"}))
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    assert resolve_token(workspace, vault) == "xoxp-real"


def test_resolve_token_raises_missing_secret_naming_the_key(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text("{}")
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    with pytest.raises(MissingSecret) as caught:
        resolve_token(workspace, vault)
    assert caught.value.key == "SLACK_USER_TOKEN_GRAVITATE"


def test_load_config_malformed_toml_raises_config_error(tmp_path):
    path = tmp_path / "corpus.toml"
    path.write_text("this is not = = toml [[[")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.message
    assert caught.value.help
    assert "malformed" in caught.value.message.lower() or "error" in caught.value.message.lower()


def test_resolve_token_empty_vault_raises_missing_secret(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text("")
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    with pytest.raises(MissingSecret) as caught:
        resolve_token(workspace, vault)
    assert caught.value.key == "SLACK_USER_TOKEN_GRAVITATE"


def test_resolve_token_non_object_vault_raises_config_error(tmp_path):
    from sherpa.corpus.config import WorkspaceConfig

    vault = tmp_path / "vault.json"
    vault.write_text("[]")
    workspace = WorkspaceConfig(name="gravitate", token_key="SLACK_USER_TOKEN_GRAVITATE", me=None)
    with pytest.raises(ConfigError) as caught:
        resolve_token(workspace, vault)
    assert caught.value.message
    assert caught.value.help
    assert "non-object" in caught.value.message.lower() or "json" in caught.value.message.lower()
