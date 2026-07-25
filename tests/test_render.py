"""Unit tests for the shared AXI output boundary.

Run: uv run --with pytest --with python-toon pytest tests/test_render.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "render", Path(__file__).resolve().parent.parent / "sherpa" / "render.py"
)
render = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render)


def test_emit_writes_toon_by_default(capsys):
    render.emit({"tasks": [{"id": 1, "title": "a"}]})
    assert capsys.readouterr().out.startswith("tasks[1")


def test_emit_writes_json_when_asked(capsys):
    render.emit({"tasks": [{"id": 1}]}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"tasks": [{"id": 1}]}


def test_errors_go_to_stdout_so_the_agent_can_read_them(capsys):
    with pytest.raises(SystemExit) as exit_info:
        render.fail("video not found", help="youtube info <URL>")
    captured = capsys.readouterr()
    assert captured.out == "error: video not found\nhelp: youtube info <URL>\n"
    assert captured.err == ""
    assert exit_info.value.code == 1


def test_usage_errors_exit_two():
    with pytest.raises(SystemExit) as exit_info:
        render.fail("--title is required", usage=True)
    assert exit_info.value.code == 2


def test_short_text_is_not_truncated():
    assert render.truncate("short", limit=100) == ("short", None)


def test_truncation_reports_the_full_size():
    preview, notice = render.truncate("x" * 250, limit=100)
    assert preview == "x" * 100
    assert notice == "... (truncated, 250 chars total)"


def test_bin_line_collapses_home_to_tilde():
    assert render.bin_line(Path.home() / ".local/bin/sherpa") == "bin: ~/.local/bin/sherpa"


def _parser():
    parser = argparse.ArgumentParser(prog="demo")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--limit", type=int)
    return parser, {"list": listing}


def test_known_flags_parse_normally():
    parser, subs = _parser()
    assert render.parse_strict(parser, subs, ["list", "--state", "open"]).state == "open"


def test_unknown_flag_is_rejected_by_name_with_the_valid_set(capsys):
    parser, subs = _parser()
    with pytest.raises(SystemExit) as exit_info:
        render.parse_strict(parser, subs, ["list", "--stat", "open"])
    out = capsys.readouterr().out
    assert "--stat" in out
    assert "--state" in out and "--limit" in out
    assert exit_info.value.code == 2


def test_hash_leading_value_is_quoted_in_output(capsys):
    render.emit({"note": "#comment"})
    assert '"#comment"' in capsys.readouterr().out


def test_control_character_value_is_quoted(capsys):
    render.emit({"note": "bad\x01value"})
    out = capsys.readouterr().out
    assert out.strip().startswith("note: \"")


def test_plain_value_is_not_quoted(capsys):
    render.emit({"note": "plain text"})
    out = capsys.readouterr().out
    assert out.strip() == "note: plain text"


def test_re_executing_the_module_leaves_the_installed_quoting_patch_alone(capsys):
    already_installed = render.toon.primitives.is_safe_unquoted
    _SPEC.loader.exec_module(render)
    assert render.toon.primitives.is_safe_unquoted is already_installed
    render.emit({"note": "#comment"})
    assert '"#comment"' in capsys.readouterr().out


def test_pristine_encoder_stays_recoverable_from_the_patch():
    pristine = render.toon.primitives.is_safe_unquoted.__wrapped__
    assert pristine("#comment", ",") is True
