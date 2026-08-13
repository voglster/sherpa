"""Reader/writer for ~/.config/sherpa/config.toml.

Holds the resolved Sherpa home and the list of tools hidden from discovery.
`cli/sherpa_cli.py` ships as a standalone wheel and cannot import this module, so
it carries its own copy of the reader — keep the two in step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "sherpa" / "config.toml"


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def hidden_tools() -> set[str]:
    """Tool names withheld from `sherpa list`, `sherpa search`, and `tool_search`."""
    return set(load().get("hidden", []))


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save(config: dict) -> None:
    lines = []
    if config.get("home"):
        lines.append(f"home = {_toml_string(str(config['home']))}\n")
    if config.get("hidden"):
        array = ", ".join(_toml_string(name) for name in config["hidden"])
        lines.append(f"hidden = [{array}]\n")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("".join(lines))


def set_hidden(names: list[str]) -> None:
    config = load()
    config["hidden"] = sorted(set(names))
    save(config)
