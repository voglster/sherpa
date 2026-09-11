"""Corpus configuration and per-workspace token resolution.

Tokens live in the sherpa vault under a per-workspace key, extending the
SLACK_USER_TOKEN convention slack_messenger already uses rather than adding a
second secrets path.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".sherpa" / "corpus.toml"
DEFAULT_VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


class ConfigError(Exception):
    def __init__(self, message: str, help: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.help = help


class MissingSecret(ConfigError):
    def __init__(self, key: str) -> None:
        super().__init__(
            f"missing secret {key}",
            help=f"sherpa vault_manager set {key} <xoxp-token>",
        )
        self.key = key


@dataclass(frozen=True)
class WorkspaceConfig:
    name: str
    token_key: str
    me: str | None = None


@dataclass(frozen=True)
class Config:
    db: Path
    workspaces: list[WorkspaceConfig]
    stale_after_hours: float = 24.0


def load_config(path: Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    if not path.exists():
        raise ConfigError(
            f"no corpus.toml at {path}",
            help="sherpa corpus status  (after creating ~/.sherpa/corpus.toml)",
        )

    raw = tomllib.loads(path.read_text())
    workspaces = []
    for name, entry in raw.get("workspace", {}).items():
        token_key = entry.get("token_key")
        if not token_key:
            raise ConfigError(
                f"workspace '{name}' has no token_key",
                help=f"add token_key = \"SLACK_USER_TOKEN_{name.upper()}\" to {path}",
            )
        workspaces.append(WorkspaceConfig(name=name, token_key=token_key, me=entry.get("me")))

    if not workspaces:
        raise ConfigError(
            f"no workspaces configured in {path}",
            help=f"add a [workspace.<name>] section to {path}",
        )

    return Config(
        db=Path(raw.get("db", "~/.sherpa/corpus.db")).expanduser(),
        workspaces=workspaces,
        stale_after_hours=float(raw.get("stale_after_hours", 24.0)),
    )


def resolve_token(workspace: WorkspaceConfig, vault_path: Path | None = None) -> str:
    path = Path(vault_path or DEFAULT_VAULT_PATH).expanduser()
    vault = json.loads(path.read_text()) if path.exists() else {}
    token = vault.get(workspace.token_key)
    if not token:
        raise MissingSecret(workspace.token_key)
    return token
