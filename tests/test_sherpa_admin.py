"""Unit tests for sherpa/config.py and the sherpa_admin tool.

Run: uv run --with pytest --with python-toon==0.1.3 pytest tests/test_sherpa_admin.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa import config as sherpa_config

_SPEC = importlib.util.spec_from_file_location(
    "sherpa_admin", Path(__file__).resolve().parent.parent / "tools" / "sherpa_admin.py"
)
sherpa_admin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sherpa_admin)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(sherpa_config, "CONFIG_PATH", path)
    return path


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    directory = tmp_path / "tools"
    directory.mkdir()
    for name in ("youtube", "notify", "jira_issues"):
        (directory / f"{name}.py").write_text("")
    monkeypatch.setattr(sherpa_admin, "TOOLS_DIR", directory)
    return directory


# --- sherpa/config.py ---


def test_hidden_tools_is_empty_without_a_config(config_file):
    assert sherpa_config.hidden_tools() == set()


def test_hidden_tools_reads_the_hidden_array(config_file):
    config_file.write_text('home = "/srv/sherpa"\nhidden = ["youtube", "notify"]\n')

    assert sherpa_config.hidden_tools() == {"youtube", "notify"}


def test_set_hidden_preserves_home(config_file):
    config_file.write_text('home = "/srv/sherpa"\n')

    sherpa_config.set_hidden(["youtube"])

    assert sherpa_config.load() == {"home": "/srv/sherpa", "hidden": ["youtube"]}


def test_set_hidden_writes_reparseable_toml_for_awkward_names(config_file):
    sherpa_config.set_hidden(['we"ird', "back\\slash"])

    assert sherpa_config.hidden_tools() == {'we"ird', "back\\slash"}


def test_empty_hidden_list_drops_the_key(config_file):
    config_file.write_text('home = "/srv/sherpa"\nhidden = ["youtube"]\n')

    sherpa_config.set_hidden([])

    assert "hidden" not in config_file.read_text()
    assert sherpa_config.load() == {"home": "/srv/sherpa"}


# --- the tool ---


def run(argv):
    return sherpa_admin.main(argv)


def test_hide_records_tools_sorted_and_deduped(config_file, tools_dir, capsys):
    run(["hide", "youtube", "notify", "youtube"])

    assert sherpa_config.hidden_tools() == {"youtube", "notify"}
    out = capsys.readouterr().out
    assert "notify,youtube" in out.replace(", ", ",") or "notify" in out


def test_hide_is_idempotent(config_file, tools_dir):
    run(["hide", "youtube"])
    run(["hide", "youtube"])

    assert sherpa_config.load()["hidden"] == ["youtube"]


def test_show_unhides(config_file, tools_dir):
    run(["hide", "youtube", "notify"])
    run(["show", "youtube"])

    assert sherpa_config.hidden_tools() == {"notify"}


def test_hiding_an_unknown_tool_exits_2_and_changes_nothing(config_file, tools_dir):
    with pytest.raises(SystemExit) as exit_info:
        run(["hide", "nosuchtool"])

    assert exit_info.value.code == 2
    assert sherpa_config.hidden_tools() == set()


def test_showing_a_tool_that_is_not_hidden_is_a_no_op_success(config_file, tools_dir):
    run(["show", "youtube"])

    assert sherpa_config.hidden_tools() == set()


def test_list_hides_hidden_tools_by_default(config_file, tools_dir, capsys):
    run(["hide", "youtube"])
    capsys.readouterr()

    run(["list"])

    out = capsys.readouterr().out
    assert "notify" in out and "jira_issues" in out
    assert "youtube" not in out


def test_list_all_marks_hidden_tools(config_file, tools_dir, capsys):
    run(["hide", "youtube"])
    capsys.readouterr()

    run(["list", "--all"])

    out = capsys.readouterr().out
    assert "youtube" in out
    assert "true" in out.lower()


def test_status_counts_active_and_hidden(config_file, tools_dir, capsys):
    run(["hide", "youtube"])
    capsys.readouterr()

    run(["status", "--json"])

    import json
    status = json.loads(capsys.readouterr().out)
    assert status["tools"] == 3
    assert status["hidden"] == 1
    assert status["active"] == 2


def test_unknown_flag_exits_2(config_file, tools_dir):
    with pytest.raises(SystemExit) as exit_info:
        run(["list", "--evrything"])

    assert exit_info.value.code == 2
