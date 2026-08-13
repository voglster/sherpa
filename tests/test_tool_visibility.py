"""Hidden tools disappear from discovery — in the CLI and in the MCP server — but still run.

Run: uv run --with pytest pytest tests/test_tool_visibility.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_SPEC = importlib.util.spec_from_file_location("sherpa_cli", _ROOT / "cli" / "sherpa_cli.py")
sherpa_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sherpa_cli)


TOOL_TEMPLATE = '"""\nname: {name}\ndescription: {description}\ncategories: [{category}]\n"""\n'


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Sherpa home with three tools and a config file the CLI will read."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, description, category in [
        ("youtube", "Fetch video transcripts", "media"),
        ("notify", "Send a desktop notification", "system"),
        ("jira_issues", "Search Jira issues", "jira"),
    ]:
        (tools / f"{name}.py").write_text(
            TOOL_TEMPLATE.format(name=name, description=description, category=category)
        )
    monkeypatch.setenv("SHERPA_HOME", str(tmp_path))
    monkeypatch.setattr(sherpa_cli, "CONFIG_PATH", tmp_path / "config.toml")
    return tmp_path


def hide(home_path, *names):
    array = ", ".join(f'"{n}"' for n in names)
    (home_path / "config.toml").write_text(f'home = "{home_path}"\nhidden = [{array}]\n')


# --- CLI ---


def test_list_omits_hidden_tools_and_says_how_many(home, capsys):
    hide(home, "youtube")

    sherpa_cli.cmd_list([])

    out = capsys.readouterr().out
    assert "notify" in out and "jira_issues" in out
    assert "youtube" not in out
    assert "1 hidden" in out


def test_list_says_nothing_about_hiding_when_nothing_is_hidden(home, capsys):
    sherpa_cli.cmd_list([])

    assert "hidden" not in capsys.readouterr().out


def test_list_all_shows_hidden_tools_marked(home, capsys):
    hide(home, "youtube")

    sherpa_cli.cmd_list(["--all"])

    out = capsys.readouterr().out
    assert "youtube" in out and "[hidden]" in out


def test_search_skips_hidden_tools(home, capsys):
    hide(home, "youtube")

    sherpa_cli.cmd_search(["video"])

    assert "youtube" not in capsys.readouterr().out


def test_search_all_finds_hidden_tools(home, capsys):
    hide(home, "youtube")

    sherpa_cli.cmd_search(["--all", "video"])

    assert "youtube" in capsys.readouterr().out


def test_hidden_tools_still_resolve_for_running_and_help(home):
    hide(home, "youtube")

    assert sherpa_cli._resolve_tool(home, "youtube").name == "youtube.py"


def test_set_home_preserves_the_hidden_list(home, capsys):
    hide(home, "youtube")

    sherpa_cli.cmd_set_home([str(home)])

    text = (home / "config.toml").read_text()
    assert f'home = "{home}"' in text
    assert '"youtube"' in text


# --- MCP server ---


def test_tool_search_omits_hidden_tools(monkeypatch):
    from sherpa import config as sherpa_config
    from sherpa import server

    tools = [
        {"name": "youtube", "description": "Fetch video transcripts", "categories": ["media"]},
        {"name": "notify", "description": "Send a video notification", "categories": ["system"]},
    ]
    monkeypatch.setattr(server, "index_if_changed", lambda: False)
    monkeypatch.setattr(server, "get_all_tools", lambda: tools)
    monkeypatch.setattr(server, "get_all_workflows", list)
    monkeypatch.setattr(sherpa_config, "hidden_tools", lambda: {"youtube"})

    search = getattr(server.tool_search, "fn", server.tool_search)
    result = search("video")

    assert [r["name"] for r in result["results"]] == ["notify"]
    assert result["total"] == 1
