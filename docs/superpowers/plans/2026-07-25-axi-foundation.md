# AXI Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sherpa's tool contract AXI-conformant, prove TOON's token savings on real sherpa payloads, and convert two pilot tools.

**Architecture:** A shared `sherpa/render.py` wraps the `python-toon` encoder and the AXI output conventions (structured errors on stdout, truncation notices, strict flag rejection). Tools are standalone PEP 723 scripts run via `uv run --script`, so they reach the shared module with a `sys.path` insert to the repo root and declare `python-toon` in their own dependency block. Internal tool logic keeps using dicts; TOON happens only at the output boundary.

**Tech Stack:** Python 3.11+, uv, PEP 723 inline script metadata, `python-toon`, pytest, Anthropic `count_tokens` API for measurement.

## Global Constraints

- Every tool keeps its PEP 723 block: `requires-python = ">=3.11"`, deps listed explicitly.
- Tools are invoked as `uv run --script tools/<name>.py` by `cli/sherpa_cli.py:185`. They must remain standalone-runnable.
- The YAML docstring contract is unchanged: `name` must match the filename, plus `description`, `categories`, optional `secrets` and `usage`.
- Exit codes after this work: `0` success including idempotent no-ops, `1` error, `2` usage error.
- Structured output and errors go to **stdout**. stderr is diagnostics only.
- Every converted tool keeps a `--json` flag emitting the pre-conversion JSON shape.
- Commit messages: no "Generated with Claude Code" or "Co-Authored-By: Claude" lines.
- Comments only where naming and structure cannot carry the intent; no `# Arrange/# Act/# Assert` narration in tests, and no comments restating what a good test name already says.
- Verified facts to build on, do not re-litigate: `toon-format` on PyPI is a **stub** whose encoder raises `NotImplementedError` — do not use it. `python-toon` installs as module `toon`, exposes `encode(value, options=None)`, and quotes delimiter-containing fields correctly. Sibling imports from `tools/*.py` into `sherpa/` work under `uv run --script`.
- `python-toon` emits array headers as `tasks[2,]{id,title}:` — the trailing delimiter marker is legal per TOON SPEC §6 (`key[N<delim?>]{fields}:`). Do not try to suppress it.
- `<pinned>` throughout this plan means the exact `python-toon` version Task 1 settles on. Task 1 must be complete before any later task copies a dependency string.

---

### Task 1: Vet `python-toon` against the official reference test suite

The whole plan rests on this dependency being correct. TOON ships a reference test suite; run it before building on the package. There is also an unresolved version discrepancy: PyPI reports `0.1.3` as latest, but an unpinned `uv run --with python-toon` resolved `0.1.1`. Resolve and pin.

**Files:**
- Create: `tests/test_toon_conformance.py`
- Create: `docs/axi-toon-decision.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a pinned dependency string (e.g. `python-toon==0.1.3`) that every later task copies verbatim into PEP 723 blocks, and the decision record justifying it.

- [ ] **Step 1: Find out why the resolver picked 0.1.1 and what the latest usable version is**

```bash
uv pip index versions python-toon
uv run --with 'python-toon==0.1.3' python -c "import toon; print(toon.__version__)"
```

Record the outcome. If `0.1.3` installs and imports, pin that. If it cannot install on Python 3.11+, pin the newest version that can and note the reason.

- [ ] **Step 2: Fetch the reference test suite**

```bash
curl -sL https://github.com/toon-format/spec/archive/refs/heads/main.tar.gz \
  | tar -xz -C /tmp --wildcards '*/tests/*'
ls /tmp/spec-main/tests
```

Inspect the fixture format before writing the test — it drives how Step 3 loads cases. If the suite's shape does not lend itself to a data-driven loader, write the conformance test against the encoder examples in SPEC.md §7.2 (quoting rules), §8 (objects), and §9.1/§9.3 (primitive and tabular arrays) instead, and say so in the decision doc.

- [ ] **Step 3: Write the conformance test**

```python
"""Conformance check for the python-toon encoder against the TOON spec.

Run: uv run --with pytest --with python-toon pytest tests/test_toon_conformance.py
"""

from __future__ import annotations

import pytest
import toon


def test_uniform_object_array_uses_tabular_form():
    encoded = toon.encode({"tasks": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]})
    header, *rows = encoded.splitlines()
    assert header.startswith("tasks[2")
    assert header.endswith("]{id,title}:")
    assert [row.strip() for row in rows] == ["1,a", "2,b"]


def test_delimiter_bearing_values_are_quoted():
    encoded = toon.encode({"rows": [{"v": "a,b"}]})
    assert '"a,b"' in encoded


def test_plain_values_are_not_quoted():
    assert '"' not in toon.encode({"rows": [{"v": "plain text"}]})


def test_nested_objects_indent_rather_than_flatten():
    encoded = toon.encode({"task": {"number": 42, "state": "open"}})
    assert encoded.splitlines() == ["task:", "  number: 42", "  state: open"]


def test_none_is_not_rendered_as_python_repr():
    assert "None" not in toon.encode({"rows": [{"v": None}]})


@pytest.mark.parametrize("hostile", ['has "quotes"', "has\nnewline", "-leading hyphen", "#leading hash"])
def test_values_needing_quotes_round_trip(hostile):
    assert toon.decode(toon.encode({"rows": [{"v": hostile}]})) == {"rows": [{"v": hostile}]}
```

- [ ] **Step 4: Run the conformance test**

Run: `uv run --with pytest --with 'python-toon==<pinned>' pytest tests/test_toon_conformance.py -v`

Expected: all pass. **This is a decision gate.** If `test_delimiter_bearing_values_are_quoted`, `test_plain_values_are_not_quoted`, or any round-trip case fails, stop and report before writing `render.py` — a package that over-quotes silently destroys TOON's token advantage, and a package that under-quotes emits corrupt output. Do not paper over a failure with a wrapper.

- [ ] **Step 5: Write the decision record**

Create `docs/axi-toon-decision.md` stating: the pinned version and why, that `toon-format` was rejected as a stub, which conformance cases pass, and any spec deviation found (including the `[N,]` header marker, which is expected and legal).

- [ ] **Step 6: Commit**

```bash
git add tests/test_toon_conformance.py docs/axi-toon-decision.md
git commit -m "test: vet python-toon against TOON spec before adopting it"
```

---

### Task 2: Shared output boundary — `sherpa/render.py`

**Files:**
- Create: `sherpa/render.py`
- Create: `tests/test_render.py`

**Interfaces:**
- Consumes: `toon.encode` from Task 1's pinned package.
- Produces, relied on by Tasks 4 and 5 exactly as named:
  - `emit(payload: dict, *, as_json: bool = False) -> None`
  - `fail(message: str, *, help: str | None = None, usage: bool = False) -> NoReturn`
  - `truncate(text: str, limit: int = 1000) -> tuple[str, str | None]`
  - `bin_line(executable: str | Path) -> str`
  - `parse_strict(parser, subparsers: dict[str, ArgumentParser] | None = None, argv: list[str] | None = None) -> Namespace`
  - `TOOL_PREAMBLE: str` — the three-line `sys.path` insert snippet tools copy.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest --with python-toon pytest tests/test_render.py -v`
Expected: FAIL at collection — `sherpa/render.py` does not exist.

- [ ] **Step 3: Implement `sherpa/render.py`**

```python
"""AXI-conformant output boundary shared by sherpa tools.

Tools run standalone under `uv run --script`, so they reach this module by
inserting the repo root on sys.path (see TOOL_PREAMBLE) and declaring
python-toon in their own PEP 723 dependency block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import toon

TOOL_PREAMBLE = """sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate"""


def emit(payload: dict[str, Any], *, as_json: bool = False) -> None:
    print(json.dumps(payload, indent=2) if as_json else toon.encode(payload))


def fail(message: str, *, help: str | None = None, usage: bool = False) -> NoReturn:
    print(f"error: {message}")
    if help:
        print(f"help: {help}")
    sys.exit(2 if usage else 1)


def truncate(text: str, limit: int = 1000) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None
    return text[:limit], f"... (truncated, {len(text)} chars total)"


def bin_line(executable: str | Path) -> str:
    path = Path(executable)
    try:
        return f"bin: ~/{path.relative_to(Path.home())}"
    except ValueError:
        return f"bin: {path}"


def parse_strict(
    parser: argparse.ArgumentParser,
    subparsers: dict[str, argparse.ArgumentParser] | None = None,
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse argv, rejecting unrecognized flags by name instead of ignoring them.

    argparse reports extras against the top-level parser, which would list the
    wrong flag set, so extras are attributed back to the chosen subcommand.
    """
    args, extras = parser.parse_known_args(argv)
    if not extras:
        return args

    command = getattr(args, "command", None)
    target = (subparsers or {}).get(command, parser)
    valid = sorted(
        option for action in target._actions for option in action.option_strings
    )
    scope = f" for `{command}`" if command else ""
    fail(
        f"unknown flag {extras[0]}{scope}",
        help=f"valid flags{scope}: {', '.join(valid)}",
        usage=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest --with python-toon pytest tests/test_render.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sherpa/render.py tests/test_render.py
git commit -m "feat(render): add AXI output boundary shared by sherpa tools"
```

---

### Task 3: Rewrite the tool contract

Do this before converting any tool, so conversions have a written target and later tools are born conformant.

**Files:**
- Modify: `docs/SHERPA_STANDARDS.md` (the "File Structure", "Conventions", and "New Tool Checklist" sections)

**Interfaces:**
- Consumes: the `render` API from Task 2, quoted in the template.
- Produces: the `axi: true` docstring marker that Task 6 reads to report per-tool conformance.

- [ ] **Step 1: Replace the template and Conventions section**

The template becomes:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==<pinned>"]
# ///
"""
name: my_tool_name
description: One-line summary of what this tool does.
categories: [category1, category2]
axi: true
usage: |
  subcommand <POSITIONAL_ARG> [--flag VALUE]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate
```

Rewrite Conventions to state:

- **stdout is TOON**, produced only through `emit()`. Every tool accepts `--json` for the previous shape.
- **Exit codes**: 0 success including idempotent no-ops, 1 error, 2 usage error. (Note in the doc that this **inverts** the old 1/2 meanings.)
- **Errors go to stdout** via `fail()`, with an actionable `help:` line naming a sherpa command. Translate dependency errors; never leak tracebacks or the wrapped tool's name.
- **stderr is diagnostics only.** Never mix progress into stdout — an agent reads "Fetching..." as data.
- **Minimal default schemas**: 3-4 fields in lists. Long-form content lives in detail views. Offer `--fields` for more.
- **Truncate, never omit**: use `truncate()`, include the total size, and suggest `--full` only when truncation happened.
- **Report true totals**: `count: 30 of 847 total`, so the agent does not paginate to find out.
- **Definitive empty states**: state the zero with context.
- **Reject unknown flags** by name via `parse_strict()` before any dependency call, exit 2, listing the valid set.
- **No interactive prompts.** Every operation completable by flags alone; suppress prompts from wrapped tools.
- **No-args prints live content** plus `bin:` and `description:` lines — not a usage manual.
- **Contextual hints** on list and mutation output, omitted on detail views; dynamic values as `<id>` placeholders, never guessed.

- [ ] **Step 2: Add the compliance checklist**

Append a checklist a reviewer can run against any tool: TOON via `emit`, `--json` present, exit codes correct, errors on stdout with help, list schema ≤4 fields, totals reported, empty state explicit, unknown flags rejected, no-args shows content, hints on lists only, `axi: true` in the docstring.

- [ ] **Step 3: Note the migration state convention**

Document that `axi: true` marks a converted tool and its absence means unconverted, so a partially migrated suite stays legible.

- [ ] **Step 4: Surface conformance in `sherpa list`**

A half-migrated suite is only safe if the migration's state is visible. Make the marker reachable from the CLI.

Read `sherpa/indexer.py:_parse_metadata` (invoked from `cli/sherpa_cli.py:59`) and confirm whether unrecognized docstring keys such as `axi` survive into `metadata.json`. If they are dropped, add `axi` to the retained field set.

Then modify `cmd_list` in `cli/sherpa_cli.py:109` to mark conformant tools — an `axi` column or a trailing marker on the name. Verify:

```bash
sherpa reindex && sherpa list
```

Expected: nothing marked yet, because no tool has been converted. After Task 4, `youtube` is marked; after Task 5, `jira_issues`. Re-run this command at the end of both tasks as the conformance check.

- [ ] **Step 5: Commit**

```bash
git add docs/SHERPA_STANDARDS.md cli/sherpa_cli.py sherpa/indexer.py
git commit -m "docs: adopt the AXI contract as sherpa's tool standard"
```

---

### Task 4: Convert `youtube` (detail-shaped pilot)

Chosen because it exercises truncation with total size, a nested chapter array, and the no-args home view — and it currently violates four of the ten principles.

**Files:**
- Modify: `tools/youtube.py`
- Modify: `tests/test_youtube.py` (add output-shape tests; the 20 existing parser tests must keep passing untouched)

**Interfaces:**
- Consumes: `emit`, `fail`, `truncate`, `parse_strict` from Task 2.
- Produces: the conversion pattern Task 5 follows.

- [ ] **Step 1: Write the failing output-shape tests**

Append to `tests/test_youtube.py`:

```python
def test_no_args_shows_content_not_help(capsys, monkeypatch):
    monkeypatch.setattr(youtube.sys, "argv", ["youtube"])
    with pytest.raises(SystemExit) as exit_info:
        youtube.main()
    out = capsys.readouterr().out
    assert "bin: " in out and "description: " in out
    assert "usage:" not in out
    assert exit_info.value.code == 0


def test_missing_captions_error_lands_on_stdout(capsys):
    with pytest.raises(SystemExit) as exit_info:
        youtube.fail("no English captions available for abc123", help="youtube info abc123")
    captured = capsys.readouterr()
    assert captured.out.startswith("error: ")
    assert captured.err == ""
    assert exit_info.value.code == 1


def test_long_description_is_truncated_with_its_total_size():
    preview, notice = youtube.truncate("x" * 5000, limit=1000)
    assert len(preview) == 1000
    assert "5000 chars total" in notice


def test_unknown_flag_is_rejected_rather_than_ignored(capsys):
    with pytest.raises(SystemExit) as exit_info:
        youtube.main(["info", "abc12345678", "--referesh"])
    assert "--referesh" in capsys.readouterr().out
    assert exit_info.value.code == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --with pytest --with python-toon pytest tests/test_youtube.py -v`
Expected: the four new tests FAIL (no-args prints help and exits 1; errors go to stderr; `truncate` and a `main(argv)` signature do not exist). The 20 existing tests PASS.

- [ ] **Step 3: Convert the tool**

Changes to `tools/youtube.py`:

1. Add `python-toon==<pinned>` to the PEP 723 dependencies and `axi: true` to the docstring.
2. Add the `sys.path` insert and import `emit, fail, parse_strict, truncate` from `sherpa.render`; delete the local `json.dumps` printing.
3. Change `main()` to `main(argv: list[str] | None = None)` and parse via `parse_strict(parser, subparsers, argv)`, keeping a dict of the four subparsers.
4. Replace every `print(..., file=sys.stderr); sys.exit(N)` with `fail(message, help=...)` — and **swap the exit codes**: missing yt-dlp becomes `usage=True` (2), fetch and caption failures become plain `fail` (1).
5. Replace each `print(json.dumps(payload, indent=2))` with `emit(payload, as_json=args.json)`; add `--json` to all four subcommands.
6. In `cmd_info`, replace the bare `[:4000]` description slice with `truncate(description, 1000)`, adding the notice to the payload and a `help:` hint naming `youtube info <id> --full`; add the `--full` flag.
7. Add a no-args home view printing `bin_line(sys.argv[0])`, a one-line description, the cached transcript count from `CACHE_DIR`, and two hints — then exit 0.
8. Trim `summarize()` to the AXI default schema and move the rest behind `--fields`. Keep `chapters` — it is the reason to call the tool.

- [ ] **Step 4: Run the full file**

Run: `uv run --with pytest --with python-toon pytest tests/test_youtube.py -v`
Expected: all PASS, including the original 20.

- [ ] **Step 5: Verify against the live API**

```bash
sherpa youtube                                  # home view, exit 0
sherpa youtube info iQyg-KypKAA                 # TOON, truncated description + notice
sherpa youtube info iQyg-KypKAA --json          # previous JSON shape
sherpa youtube transcript iQyg-KypKAA           # cache hit
sherpa youtube info iQyg-KypKAA --referesh      # error: unknown flag, exit 2
sherpa youtube info zzzzzzzzzzz; echo "exit=$?" # error on stdout, exit 1
```

Confirm no progress text reaches stdout and that TOON output has no stray quoting.

- [ ] **Step 6: Commit**

```bash
git add tools/youtube.py tests/test_youtube.py
git commit -m "feat(youtube): convert to the AXI contract"
```

---

### Task 5: Convert `jira_issues search` (list-shaped pilot)

The largest real payload in the suite, and the one where minimal schemas and true totals pay off most.

**Files:**
- Modify: `tools/jira_issues.py` (the `search` subcommand and its output path only)
- Create: `tests/test_jira_issues.py`

**Interfaces:**
- Consumes: `emit`, `fail`, `truncate`, `parse_strict` from Task 2.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Read the current search implementation**

Read `tools/jira_issues.py` and note the exact field set `search` returns today and the total-count field the Jira API provides (`total` in the v2/v3 search response). Do not guess the response shape — read the code.

- [ ] **Step 2: Write the failing tests**

The Jira API is not available in tests, so test the pure output-shaping functions. Extract them if they are currently inline — a `search_rows(issues) -> list[dict]` and a `search_payload(issues, total) -> dict` are the units worth testing.

```python
"""Output-shape tests for the jira_issues AXI conversion.

Run: uv run --with pytest --with python-toon pytest tests/test_jira_issues.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "jira_issues", Path(__file__).resolve().parent.parent / "tools" / "jira_issues.py"
)
jira_issues = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jira_issues)

ISSUE = {
    "key": "KB-1",
    "fields": {
        "summary": "Fix auth bug",
        "status": {"name": "Open"},
        "assignee": {"displayName": "Jane Doe"},
        "description": "y" * 4000,
    },
}


def test_default_row_stays_within_the_axi_field_budget():
    assert set(jira_issues.search_rows([ISSUE])[0]) == {"key", "summary", "status"}


def test_long_descriptions_never_reach_list_output():
    assert "y" * 100 not in str(jira_issues.search_rows([ISSUE]))


def test_payload_reports_the_true_total_not_the_page_size():
    payload = jira_issues.search_payload([ISSUE], total=847)
    assert payload["count"] == "1 of 847 total"


def test_empty_result_states_the_zero_explicitly():
    payload = jira_issues.search_payload([], total=0)
    assert "0" in str(payload["issues"])
    assert payload.get("help")


def test_hints_use_placeholders_rather_than_guessed_values():
    hints = jira_issues.search_payload([ISSUE], total=1)["help"]
    assert any("<" in hint for hint in hints)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --with pytest --with python-toon pytest tests/test_jira_issues.py -v`
Expected: FAIL — `search_rows` and `search_payload` do not exist yet.

- [ ] **Step 4: Implement**

1. Add `python-toon==<pinned>` to dependencies, `axi: true` to the docstring, the `sys.path` insert, and the `render` imports.
2. Extract `search_rows(issues)` returning `{key, summary, status}` per issue, and `search_payload(issues, total)` returning `{"count": f"{len(issues)} of {total} total", "issues": rows, "help": [...]}`.
3. On an empty result, set `issues` to an explicit zero statement rather than an empty array, and keep a hint suggesting a broader query.
4. Hints use placeholders: `Run 'sherpa jira_issues get <ISSUE_KEY>' for full detail`.
5. Add `--fields` to opt into extra columns and `--json` for the old shape.
6. Route every error through `fail()` with a `help:` line, swapping exit codes to 1 = error / 2 = usage. Missing vault secrets become `usage=True`, since the fix is user action, not a retry.
7. Convert `search`'s output to `emit(payload, as_json=args.json)`. Leave the other subcommands unconverted — they are follow-on work.

- [ ] **Step 5: Run the tests**

Run: `uv run --with pytest --with python-toon pytest tests/test_jira_issues.py -v`
Expected: all PASS.

- [ ] **Step 6: Verify against live Jira**

```bash
sherpa jira_issues search --mine
sherpa jira_issues search --mine --json
sherpa jira_issues search --jql 'project = KB AND status = "Nonexistent"'   # explicit zero
sherpa jira_issues search --mine --limitt 5; echo "exit=$?"                  # unknown flag, exit 2
```

- [ ] **Step 7: Commit**

```bash
git add tools/jira_issues.py tests/test_jira_issues.py
git commit -m "feat(jira_issues): convert search to the AXI contract"
```

---

### Task 6: Measure the token savings

The 40% figure is published by AXI's author, measured on his tools. Check it on sherpa payloads, because the answer decides whether converting the remaining tools is worth doing.

**Files:**
- Create: `scripts/measure_toon.py`
- Create: `docs/axi-measurement.md`

**Interfaces:**
- Consumes: the converted `--json` and TOON paths from Tasks 4 and 5.
- Produces: the measurement report that gates fan-out.

- [ ] **Step 1: Write the measurement script**

Count tokens with the tokenizer that actually bills, not a `tiktoken` approximation. The script takes captured payloads, encodes each as both JSON and TOON, and reports the delta.

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==<pinned>", "anthropic"]
# ///
"""Compare JSON and TOON token counts for real sherpa payloads.

Run: uv run --script scripts/measure_toon.py <payload.json> [...]
Requires ANTHROPIC_API_KEY in the environment or the sherpa vault.
"""

import json
import sys
from pathlib import Path

import anthropic
import toon

MODEL = "claude-opus-5"


def count(client: anthropic.Anthropic, text: str) -> int:
    result = client.messages.count_tokens(
        model=MODEL, messages=[{"role": "user", "content": text}]
    )
    return result.input_tokens


def main() -> None:
    if len(sys.argv) < 2:
        print("error: no payloads given")
        print("help: uv run --script scripts/measure_toon.py <payload.json> [...]")
        sys.exit(2)

    client = anthropic.Anthropic()
    rows = []
    for path in sys.argv[1:]:
        payload = json.loads(Path(path).read_text())
        as_json = json.dumps(payload, indent=2)
        as_toon = toon.encode(payload)
        json_tokens, toon_tokens = count(client, as_json), count(client, as_toon)
        rows.append(
            {
                "payload": Path(path).stem,
                "json": json_tokens,
                "toon": toon_tokens,
                "saved": f"{(json_tokens - toon_tokens) / json_tokens:.1%}",
            }
        )

    print(toon.encode({"measurements": rows}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Capture three payload sizes per pilot**

Savings scale with row count, so one sample would mislead.

```bash
mkdir -p /tmp/axi-payloads
sherpa youtube info iQyg-KypKAA --json      > /tmp/axi-payloads/youtube-detail.json
sherpa jira_issues search --mine --json     > /tmp/axi-payloads/jira-typical.json
sherpa jira_issues search --jql 'project = KB ORDER BY created DESC' --limit 100 --json \
                                            > /tmp/axi-payloads/jira-large.json
```

- [ ] **Step 3: Run the measurement**

```bash
uv run --script scripts/measure_toon.py /tmp/axi-payloads/*.json
```

- [ ] **Step 4: Write the report**

Create `docs/axi-measurement.md` with the per-payload table and a recommendation on converting the remaining tools. Record the honest result: savings on a one-row detail view will be far below 40% because TOON's advantage comes from not repeating keys per row, and saying so is the useful finding. If large lists land well below 40%, say that too and recommend against a full fan-out.

- [ ] **Step 5: Commit**

```bash
git add scripts/measure_toon.py docs/axi-measurement.md
git commit -m "test: measure TOON savings on real sherpa payloads"
```

---

### Task 7: Catalog triage table

**Files:**
- Create: `docs/axi-triage.md`

**Interfaces:**
- Consumes: the measurement from Task 6.
- Produces: the decision record that makes fan-out mechanical.

- [ ] **Step 1: List the current tools**

```bash
sherpa list
```

- [ ] **Step 2: Write the table**

One row per tool with columns: tool, decision (`axi-ify` / `replace` / `leave`), existing AXI if any, and rationale. Expected starting point, to be checked rather than assumed:

- **axi-ify**: `jira_issues`, `jira_admin`, `jira_pulse`, `fleet`, `knowledge`, `notes_search`, `lumbergh`, `youtube`, `sentry_issues`, `notify`, `vault_manager`, `ask_ai`, `image_edit`, `image_gen`, `unsplash_search`, `client_db`, `slack_messenger`, `slack_pomodoro`
- **leave**: `reindex`, `web_read`, `web_search` — thin enough that conversion is not worth it
- **replace candidates**: `slack_messenger` → `slack-axi`, `client_db` → `mongodb-axi`

For each replace candidate, list the local features the replacement must cover before a swap is safe, and mark it `axi-ify` instead if it does not: `slack_messenger` has `@(name)` mention resolution, Jira key auto-linking, and fuzzy user/channel caching; `client_db` has client-instance search and read-only credential separation. Do not recommend a swap without checking.

- [ ] **Step 3: Commit**

```bash
git add docs/axi-triage.md
git commit -m "docs: triage sherpa tools against the AXI catalog"
```

---

## Deferred to later phases

Not in this plan: converting the remaining tools (gated on Task 6), AXI §7 session hooks, and phases B (no-mistakes), C (lavish in lumbergh), D (ephemeral worktrees), E (skill audit).
