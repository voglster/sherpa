"""`sherpa manifest` — the machine-readable index a caller authorizes against.

Run: uv run --with pytest --with pyyaml pytest tests/test_manifest.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_SPEC = importlib.util.spec_from_file_location("sherpa_cli", _ROOT / "cli" / "sherpa_cli.py")
sherpa_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sherpa_cli)

TIERS = {"read", "write", "dangerous"}


def _normalize(value):
    """Strip trailing whitespace from every string, however deeply nested."""
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _manifest(home: Path, monkeypatch) -> dict:
    monkeypatch.setenv("SHERPA_HOME", str(home))
    monkeypatch.setattr(sherpa_cli, "CONFIG_PATH", home / "config.toml")
    out = io.StringIO()
    with redirect_stdout(out):
        assert sherpa_cli.cmd_manifest([]) == 0
    return json.loads(out.getvalue())


DECLARED = '''"""
name: {name}
description: does a thing
categories: [test]
risk: high
operations:
  get:
    tier: read
    argv: ["get", "{{id}}"]
  wipe:
    tier: dangerous
    argv: ["wipe", "{{id}}"]
"""
'''

BARE = '"""\nname: {name}\ndescription: undeclared\ncategories: [test]\n"""\n'


@pytest.fixture
def home(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "declared.py").write_text(DECLARED.format(name="declared"))
    (tools / "bare.py").write_text(BARE.format(name="bare"))
    return tmp_path


def test_a_declared_tool_carries_its_tiers(home, monkeypatch):
    manifest = _manifest(home, monkeypatch)
    (tool,) = manifest["tools"]

    assert tool["name"] == "declared"
    assert tool["risk"] == "high"
    assert tool["operations"]["get"]["tier"] == "read"
    assert tool["operations"]["wipe"]["argv"] == ["wipe", "{id}"]


def test_an_undeclared_tool_is_named_rather_than_omitted(home, monkeypatch):
    """"No such tool" and "that tool has not said what it does" are different problems.

    A caller that cannot find `jira_issues` needs to know which of the two it is
    looking at — one is fixed by installing something, the other by adding six
    lines to a docstring.
    """
    manifest = _manifest(home, monkeypatch)
    assert manifest["undeclared"] == ["bare"]
    assert [t["name"] for t in manifest["tools"]] == ["declared"]


def test_hiding_is_reported_and_not_obeyed(home, monkeypatch):
    """Hidden is progressive disclosure, not access control — as the README says.

    So a hidden tool stays in the manifest and is flagged, and a consumer that
    wants to honour hiding still can. Dropping it here would quietly turn a
    discovery preference into a permission.
    """
    (home / "config.toml").write_text('hidden = ["declared"]\n')
    manifest = _manifest(home, monkeypatch)
    assert manifest["tools"][0]["hidden"] is True


# ---------------------------------------------------------------------------
# The real toolbox, against its own declarations
# ---------------------------------------------------------------------------

REAL_TOOLS = sorted((_ROOT / "tools").glob("*.py"))


@pytest.mark.parametrize("script", REAL_TOOLS, ids=lambda p: p.stem)
def test_both_docstring_parsers_agree(script):
    """The CLI reads the source text; the indexer reads the parsed docstring.

    They must return the same metadata. They did not: `slack_messenger`
    documented `\\n` in a cooked docstring, so Python expanded it into a real
    newline before `ast.get_docstring` saw it, the block scalar ended early, and
    YAML refused the whole header. The CLI's regex never noticed because it reads
    the file as text. The tool was therefore missing from the MCP index and
    present in `sherpa list`, which is the sort of disagreement nobody goes
    looking for.
    """
    source = script.read_text()

    match = sherpa_cli._DOCSTRING_RE.search(source)
    if not match:
        pytest.skip("no module docstring")
    try:
        by_text = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        pytest.fail(f"{script.name}: the CLI cannot parse this docstring: {error}")

    docstring = ast.get_docstring(ast.parse(source))
    try:
        by_ast = yaml.safe_load(docstring) if docstring else None
    except yaml.YAMLError as error:
        pytest.fail(f"{script.name}: the indexer cannot parse this docstring: {error}")

    # Compared with trailing whitespace normalised away: `ast.get_docstring`
    # cleans the docstring and the regex returns the source text verbatim, so a
    # block scalar's final newline differs on nearly every tool. That difference
    # is real and means nothing. A block that *ends early* — the failure this
    # test exists for — changes the structure, not the last character.
    assert _normalize(by_text) == _normalize(by_ast), f"{script.name}: the two parsers disagree"


@pytest.mark.parametrize("script", REAL_TOOLS, ids=lambda p: p.stem)
def test_declarations_are_well_formed(script):
    """Every declared operation, checked against the rules a caller relies on."""
    match = sherpa_cli._DOCSTRING_RE.search(script.read_text())
    if not match:
        pytest.skip("no module docstring")
    meta = yaml.safe_load(match.group(1)) or {}
    operations = meta.get("operations")
    if not operations:
        pytest.skip("undeclared")

    usage = meta.get("usage", "")
    for name, spec in operations.items():
        where = f"{script.stem}.{name}"
        assert spec.get("tier") in TIERS, f"{where}: tier {spec.get('tier')!r}"

        argv = spec.get("argv")
        assert isinstance(argv, list) and argv, f"{where}: no argv"
        assert all(isinstance(part, str) for part in argv), f"{where}: non-string in argv"

        # The subcommand decides the tier, so it cannot come from an argument.
        assert "{" not in argv[0], f"{where}: the subcommand is a placeholder"

        # And it has to be a subcommand this tool actually has. The usage block is
        # the only other place they are written down, so drift shows up as a name
        # in one and not the other.
        assert argv[0] in usage, f"{where}: {argv[0]!r} is not in the usage block"

        for arg, flag in (spec.get("optional") or {}).items():
            assert isinstance(flag, str) and flag.startswith("-"), f"{where}: {arg} -> {flag!r}"
            assert flag in usage, f"{where}: {flag} is not in the usage block"
